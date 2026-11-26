# Example submission to the template challenge

A complete, working example of submitting forecasts to a challenge **from a
GitHub repository**, built on the
challenge template (`eco4cast/challenge-template`;
project `templatecast`: daily water temperature at two reservoirs, up to
30 days ahead). Copy it as the starting point for your own model.

There is nothing to upload and no key to ask for. The challenge reads new files
from this repository, which only has to be **public**.

```
challenge-model.yaml   which model this repository submits, and its metadata
forecasts/             the forecast files (this example names them <theme>-<reference date>-<model_id>.csv.gz; any name works)
forecast.R             makes a forecast and writes the file (R)
forecast.py            the same forecast, in Python only
gefs_weather.py        reads weather (any GEFS variables) from dynamical.org (Python)
challenge_s3.py        reads a CSV from the challenge's S3 storage, with a key if it is private
cleanup_forecasts.py   deletes forecast files the challenge has already processed
check_forecast.py      checks a forecast file before you push it
AGENTS.md              guidance for an AI assistant writing and submitting forecasts here
requirements.txt       the Python packages it needs
.github/workflows/     optional: run forecast.R (or forecast.py) every day and commit the file;
                       clean up forecasts the challenge has already read
```

## The model

A regression of water temperature on air temperature, driven by an ensemble weather
forecast, so the forecast is an **ensemble** of 31 members:

1. **Weather from dynamical.org.** `gefs_weather.py` reads daily weather at each lake (by
   default the 2 m air temperature; see *Choosing weather variables* for the other 26) from
   two [dynamical.org](https://dynamical.org) datasets of the NCEP Global Ensemble Forecast
   System (GEFS):
   - the weather the lakes had over the past year, to fit the model, from the
     [**NOAA GEFS analysis**](https://dynamical.org/catalog/noaa-gefs-analysis/): the model's
     best estimate of past weather, one 3-hourly time series since 2000;
   - each member's forecast of the weather to come, from the
     [**NOAA GEFS forecast, 35 day**](https://dynamical.org/catalog/noaa-gefs-forecast-35-day/):
     31 members (a control and 30 perturbed), run every day at 00 UTC out to 35 days.

   Both are the same model, so a relationship fitted to the analysis carries over to the forecast.
2. **Fit.** At each lake, `lm(water temperature ~ air temperature)` over the past year.
3. **Forecast.** Each GEFS member's air temperature goes through the regression, plus a
   random draw from the regression's residuals, to make one member of the water temperature
   forecast (`family = ensemble`, the member number is the `parameter`). The weather
   uncertainty comes from the 31 GEFS members and the model's own from the residuals.

```sh
pip install -r requirements.txt     # Python: icechunk, xarray, zarr, pystac, numpy, pandas, boto3
Rscript forecast.R                  # R: readr and dplyr
python forecast.py                  # or the same model in Python only
```

It writes `forecasts/lakes-<GEFS run date>-example_lake_model.csv.gz`. The file in this
repository is for the 2026-11-19 run. The model is a simple one on purpose: replace the
"Forecast" section of `forecast.R` with your own (other GEFS variables, a better regression, a
process model) and keep the rest.

### Python only

`forecast.py` is the same model written in Python, for people who do not use R. It uses
`gefs_weather.py` directly (`import gefs_weather`), fits the regressions with NumPy, and
writes the same file, with the same environment variables (`REFERENCE_DATE`, `TARGETS_URL`,
`SITES_URL`, `TRAIN_DAYS`, `WEATHER_VARIABLES`, `S3_KEY` and `S3_SECRET`). With a private
bucket it reads the targets through `boto3` with the key, so it needs no extra program. The
two versions give the same regressions; the 31 members differ only in their random draws.
They write the same file name, so use one of them for a given model. To run the Python one
every day use `.github/workflows/forecast-python.yml` instead of `forecast.yml`; it needs
no container and no R.

### Choosing weather variables

GEFS has 27 variables (air temperature at 2 m and 80 m, humidity, wind components at 10, 80
and 100 m and gusts, pressure, precipitation rate, cloud cover, precipitable water, short-
and long-wave radiation, categorical rain/snow/ice, and more), the same in the analysis and
the forecast. Name the ones you want and both the Python script and `forecast.R` follow:

```sh
python3 gefs_weather.py list                      # every variable: name, units, how a step is defined

# R: the regression uses whatever you list (comma separated, one coefficient each)
WEATHER_VARIABLES=temperature_2m,downward_short_wave_radiation_flux_surface,wind_u_10m \
  Rscript forecast.R

# Python, by hand: the weather the lakes had, and each ensemble member's forecast
python3 gefs_weather.py history  sites.csv history.csv  --date 2026-10-31 --days 365 \
  --variables temperature_2m relative_humidity_2m
python3 gefs_weather.py forecast sites.csv forecast.csv --date 2026-11-20 --horizon 30 \
  --variables temperature_2m relative_humidity_2m
```

`sites.csv` needs `site_id`, `latitude` and `longitude` columns (the template challenge's
`sites.csv` works). Each variable comes back as a column of daily values: the 3- or 6-hourly
steps of each UTC day are averaged, except `maximum_temperature_2m` and
`minimum_temperature_2m`, which take the day's max and min. Add `:max`, `:min`, `:mean` or
`:sum` to a name to choose, e.g. `temperature_2m:max`, which comes back as the column
`temperature_2m_max`. Values keep the dataset's units: for example `precipitation_surface` is
a rate in kg m-2 s-1 (mm per second), so multiply its daily mean by 86400 for mm per day.
A name that is not a GEFS variable stops with a list of close matches. Only the grid cells at
your sites are downloaded, so more variables add a second or two each.

### Reading weather forecasts from dynamical.org

dynamical.org publishes weather datasets as cloud-optimized
[Zarr](https://zarr.dev) in an [Icechunk](https://icechunk.io) repository on public storage.
There is no account or key, and you only download the part you ask for. The dataset's
[STAC](https://stac.dynamical.org/noaa-gefs-forecast-35-day/collection.json) record gives
the address, and `xarray` opens it lazily:

```python
import icechunk, pystac, xarray as xr

catalog = pystac.Catalog.from_file("https://stac.dynamical.org/catalog.json")

def open_dataset(name):
    asset = catalog.get_child(name).assets["icechunk-https"]
    repo = icechunk.Repository.open(icechunk.http_storage(asset.href))
    return xr.open_zarr(repo.readonly_session("main").store, chunks=None)

# Air temperature (deg C) at the cell nearest a lake.
forecast = open_dataset("noaa-gefs-forecast-35-day")   # init_time x ensemble_member x lead_time x lat x lon
forecast["temperature_2m"].sel(latitude=29.68, longitude=-82.01, method="nearest")

analysis = open_dataset("noaa-gefs-analysis")           # time x lat x lon
analysis["temperature_2m"].sel(latitude=29.68, longitude=-82.01, method="nearest").sel(time="2026-10")
```

In the forecast, `init_time` is the day of the run, `ensemble_member` runs 0-30, `lead_time` is
3-hourly out to 10 days and 6-hourly after, and the grid is 0.25 degrees. The analysis is
3-hourly on the same grid; a year at one lake takes a second or two. The dataset also has other
variables (wind, humidity, radiation, precipitation, ...): print `ds` to see them. R
has no Icechunk reader, so `forecast.R` calls the small Python script and reads the CSV it
writes. The newest run takes a few hours to be added after 00 UTC; the script uses the latest
complete one and the file is named for it.

Weather data: NOAA NWS NCEP GEFS (forecast and analysis), processed by dynamical.org
([CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)).

## What the challenge needs from you

1. **Forecast files** (`.csv` or `.csv.gz`) in the folder named in `challenge-model.yaml`
   (`forecasts/`). **The file name does not matter**: the challenge identifies a
   forecast by its `model_id` and `reference_datetime` columns, and the model id must
   be one listed in `challenge-model.yaml`. A file can hold several variables and
   several reference dates, and the same file can go to every challenge in
   `challenge-model.yaml` (each keeps only the rows with its own variables, durations
   and sites). Columns: `model_id`, `reference_datetime`, `datetime`, `duration`,
   `site_id`, `variable`, `family`, `parameter` and `prediction`; `project_id` is an
   optional label for the challenge a row is for (leave it out for rows meant for
   every challenge). If a forecast for the same model, reference date, variable and
   duration is already stored, the first is kept. A `normal`
   forecast has a `mu` and a `sigma` row for every site and date; an ensemble
   (as here) uses `family = ensemble` or `sample` with the member number as `parameter`.
2. **`challenge-model.yaml`** at the top of the repository. It names the
   challenge (`templatecast`) and each model, and answers the questions of the
   model registration form; the challenge builds the model's catalog entry from
   it, and re-reads it when it changes. Leave out any question you do not want to
   answer.
3. **Registration.** Open the **Register a model repository** issue form on the
   challenge repository, give this repository's address (it can be on GitHub, GitLab,
   Bitbucket or Codeberg) and say that your forecasts are in the repository. A bot
   checks it, adds it to the list of repositories the challenge reads, and closes
   the issue.

After that, every new file is checked the next time the challenge processes
submissions. What happened to each file, with the reason for any rejection, is
published at
`<forecasts bucket>/submission_results/example_lake_model.csv` (the challenge's
"How to forecast" page gives the address).

## Submitting from Hugging Face (or GitLab, Bitbucket, Codeberg)

The challenge reads any public git repository, so the repository can be on another host.
On Hugging Face, make a dataset or model repository, put `challenge-model.yaml` at its top
and the forecast files in `forecasts/`, and register its address
(`https://huggingface.co/datasets/<you>/<name>`). Hugging Face stores `.gz`, `.parquet` and
files over 10 MB with Git LFS; the challenge follows those pointers, so a `.csv.gz` works.
Run the forecast wherever you like (a scheduled Space or job, or a GitHub Action that
pushes to the Hub) and commit the new file; `cleanup_forecasts.py` also handles LFS files.

## Submitting from your own computer

You do not need GitHub Actions to submit. If you make the forecast on your laptop, a
server or a cluster, a submission is a new file in `forecasts/` that you **push** to this
repository's default branch (`main`). The challenge reads the repository, not your
computer, so nothing else is uploaded. Do the registration steps above once; after that,
for each forecast:

```sh
# once: get the repository and what the scripts need
git clone https://github.com/<you>/<this-repository>.git
cd <this-repository>
pip install -r requirements.txt

# each time: bring your copy up to date, make the forecast, and push it
git pull --rebase                      # picks up anything pushed from elsewhere
python forecast.py                     # or: Rscript forecast.R, or any tool you like
git status                             # one new file in forecasts/
python check_forecast.py forecasts/lakes-2026-11-20-example_lake_model.csv.gz \
    --challenge https://raw.githubusercontent.com/rqthomas/challenge-template/main/challenge_configuration.yaml   # exit status 0 = fine
git add forecasts/lakes-2026-11-20-example_lake_model.csv.gz   # the file it wrote
git commit -m "Forecast for 2026-11-20"
git push
```

- **Any tool will do.** The only requirement is a `.csv` or `.csv.gz` file in `forecasts/`, in
  the format described above. If a model of your own makes it, save it there and `git add` it.
- **Add new files; never edit one the challenge has read.** A file is read the first time
  it is seen, and later changes to it are ignored, so a corrected forecast needs a new
  reference date (a new file). Do not force-push or rewrite history over files you
  have already pushed.
- **Check the file before you push.** `check_forecast.py --challenge <config>` (give the
  address of each challenge's `challenge_configuration.yaml`) says how many rows each
  challenge would keep and why it drops the rest. The model id must be the one in
  `challenge-model.yaml`, and the folder must be the `path` given there. A file the
  challenge cannot use is skipped or rejected, and the reason is published.
- **Signing in to GitHub.** `git push` needs your credentials: sign in with the GitHub CLI
  (`gh auth login`), use an SSH key, or give a personal access token when asked for a
  password. The repository itself must stay **public**, because the challenge reads it
  without a key.
- **If a push is rejected** ("fetch first"), something else added a commit, such as a
  scheduled workflow or another machine. Run `git pull --rebase` and push again.

The challenge picks the file up the next time it processes submissions, which it does once a day
(see *Timing and the deadline* below). To see what happened to a
file, read the list the challenge publishes for your model (the same one
`cleanup_forecasts.py` uses). It has one row per file with a `status`, and a `reason` when
a file was rejected:

```sh
python -c "from challenge_s3 import read_csv; print(read_csv('https://s3.us-east-005.backblazeb2.com/template-challenge/submission_results/example_lake_model.csv').tail())"
```

For a private bucket, set `S3_KEY` and `S3_SECRET` (a read-only key) first. A status of
`processed` means the forecast is in the challenge, and `invalid` explains what to fix
(then submit a new file). Once a file is processed you can delete it from your computer and
the repository (see *Cleaning up forecasts the challenge already has*).

To make forecasts on a schedule from your own machine, run the same commands from `cron`
(or any scheduler), for example twice a day at 05:47 and 13:47 (see *Timing and the deadline*):

```sh
0 18 * * *  cd /path/to/this-repository && git pull --rebase && python forecast.py && git add forecasts && git commit -m "Forecast" && git push
```

(The `&&` chain stops at the first failure, so a failed forecast pushes nothing.) If you
also use the workflows, they and your computer then both add files, which is fine as long
as each reference date is made in only one place.

## Things to know

- A file is read **the first time it is seen**; editing it later has no effect.
  Add a new file for a new reference date instead.
- Only the repository registered for a `model_id` can submit as it, and a model id
  belongs to the first repository that registered it.
- A model id containing `example` is a test model: it is processed like any
  other but kept out of the challenge's catalog and analyses. This
  example uses one so that it can be tried safely. Pick your own id to be
  included.
- `challenge-model.yaml`'s `code_url` still says `YOUR-ACCOUNT`; set it to this
  repository's address when you publish it.

## If the challenge's bucket is private

By default `forecast.R` reads the targets by their public URL. If the challenge keeps
its bucket private, ask its organizers for a **read-only key**, then set two
environment variables and nothing else changes:

```sh
export S3_KEY=<key id> S3_SECRET=<application key>
Rscript forecast.R        # also needs the paws.storage package
```

The script then reads the same address through the S3 API with the key: it downloads
the targets file to a temporary folder and reads that. The S3 client is the R package
`paws.storage`, so there is no program to install. In GitHub Actions add the
two values as repository secrets named `S3_KEY` and `S3_SECRET` (Settings → Secrets and
variables → Actions → New repository secret); the workflow passes them on. Never commit
a key. The workflow commits each new forecast with the repository's built-in token, so
it needs no other secret; if the push is refused, set **Settings → Actions → General →
Workflow permissions** to *Read and write permissions*.

## Cleaning up forecasts the challenge already has

A daily forecast adds a file to `forecasts/` every day. Once the challenge has read a file
it keeps its own copy, so the one here can go. `cleanup_forecasts.py` deletes the files the
challenge has processed and keeps the rest:

```sh
python cleanup_forecasts.py --dry-run    # say what would be deleted
python cleanup_forecasts.py              # delete it
python cleanup_forecasts.py --keep 3     # but keep the newest 3 forecasts the challenge has
python cleanup_forecasts.py --keep-days 7   # but keep everything from the last 7 days
```

It does not look for the forecast data themselves on the S3 storage; it reads the list the
challenge publishes of every file it has handled, one per model at
`<bucket>/submission_results/<model_id>.csv`. That list gives each file's path, its git
blob SHA (a fingerprint of its contents, which the script works out for the file here) and a
status. A file is deleted only if the list has **the same path with the same SHA and the
status `processed`**. These are kept, with the reason printed:

- a file the challenge has not processed yet (or has not seen);
- a file it rejected (status `invalid`; fix the forecast and submit a new file);
- a file that was edited after the challenge processed it (the challenge ignores such
  edits, so what is here is no longer what it has).

`--keep-days D` keeps every forecast from the last `D` days whether or not the challenge
has it, which gives you a window of recent forecasts to look at (or to resubmit from if
something went wrong). A forecast's age is today (UTC) minus its reference date,
so with `--keep-days 7` the files with a reference date 7 days ago or later stay. `--keep N` and
`--keep-days D` are independent: a forecast stays if it is among the newest `N` the
challenge has *or* is from the last `D` days. The workflow has an input for each.

If the list cannot be read (wrong address, a private bucket without a key, no network),
nothing is deleted and the script stops with an error. Files for other models, and other
files, are never touched. Set `S3_BASE_URL` for a different bucket than the template
challenge's, and `S3_KEY` and `S3_SECRET` (a read-only key) if the bucket is private.

`.github/workflows/cleanup-forecasts.yml` runs it from the Actions tab (tick *Dry run*
first) and commits the deletions; the weekly schedule in it is commented out. The
workflows that change `forecasts/` share one concurrency group, so a clean-up never races a
new forecast's commit. Deleting a file only removes it from the current files: it stays in
the repository's git history.

## Timing and the deadline

The challenge reads new files, builds its dashboard and scores forecasts in one daily run, which
GitHub's free scheduler starts at an hour that varies (4 to 12 hours late is common, and some runs
never happen). So the deadline for a forecast is a **date**, not a time of day: a forecast for
reference date D is shown as the "latest forecast" at the challenge's update on day
D + `settle_days` (its *Participate* page gives the number; the template challenge uses 3), so
commit it by the **end of day D + `settle_days` − 1 (UTC)**. A later forecast is still read and
scored; it just is not the one on those views.

This example's forecast starts from the latest weather run, which is 1 to 2 days behind, so its
reference date is a day or two before the day it is made. That is why the template challenge waits
3 days. If you run it yourself, schedule it **twice a day, at minutes that are not 0** (the
workflow does: 05:47 and 13:47 UTC): a run that starts hours late still commits before midnight,
and the second covers a run GitHub dropped. A repeat is harmless, since the same forecast writes the
same file and nothing is committed when it has not changed.

## Making a forecast every day

`.github/workflows/forecast.yml` (or `forecast-python.yml`, for `forecast.py`) installs Python and the packages in `requirements.txt`, runs
`forecast.R` and commits the new file. Its
schedule is commented out: switch it on once the repository is registered and
the challenge's targets are published, since the forecast needs them. (Without
the schedule you can run it from the Actions tab.) Anything else that puts a
new file in `forecasts/` works as well: a cron job, a pipeline, a commit by hand.
