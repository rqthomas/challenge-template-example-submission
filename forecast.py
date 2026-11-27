#!/usr/bin/env python3
"""Example forecast for the template challenge ("templatecast"), in Python only.

Daily water temperature at two reservoirs, up to 30 days ahead, as a 31-member
ensemble. This is the same model as forecast.R, for people who work in Python: a
regression of water temperature on air temperature, driven by the NOAA GEFS weather
ensemble from dynamical.org.

  1. Weather. gefs_weather.py reads daily weather at each lake (by default the 2 m air
     temperature, but any of GEFS's 27 variables: see WEATHER_VARIABLES below) from
     dynamical.org (https://dynamical.org), whose datasets anyone can read over HTTPS with
     no account. For the weather the lakes had it uses the "NOAA GEFS analysis" (the model's
     best estimate of past weather, 3-hourly since 2000); for the weather to come, the 31
     ensemble members of the "NOAA GEFS forecast, 35 day" run of the day before. Both are
     the same GEFS model, so a relationship fitted to one carries over to the other.
  2. Fit. At each lake, a linear regression of the observed water temperature on that
     weather (water ~ air temperature) over the past year.
  3. Forecast. Each GEFS member's weather goes through the regression, plus a random draw
     from the regression's residuals, to make one member of the water temperature forecast.
     The 31 members are submitted as an ensemble: the weather uncertainty comes from GEFS and
     the model's own uncertainty from the residuals.

No delay. The forecast is dated today (UTC) and uses yesterday's GEFS run, the newest one
that is complete by the time a run of this script happens, so it can be submitted the day it
is made. GEFS runs 35 days, so yesterday's run reaches 34 days after today, more than the 30 the
forecast needs; the day it shares with today is dropped. If yesterday's run is not complete yet
(the dataset is updated once a day, at about 06:45 UTC, and that update completes yesterday's
run, so run this after about 07:00 UTC) no file is made and the script exits quietly; a later run makes it.
A file that already exists is never overwritten, because the challenge ignores changes to a
file it has read.

Replace the "Forecast" section with your own model; keep the rest, which reads the targets
and the weather and writes the file in the format the challenge expects.

Run from the repository root:  python forecast.py
Needs the packages in requirements.txt (pip install -r requirements.txt).

Environment variables (all optional)
  REFERENCE_DATE  the day the forecast starts from, YYYY-MM-DD (default: today, UTC). The
                  weather is the GEFS run of the day before, and the file is named for this date.
  FORCE           set to 1 to replace a file that already exists (it is kept by default)
  TARGETS_URL     where to read the challenge's targets (default: the published file)
  SITES_URL       the challenge's site list, with each lake's latitude and longitude
  TRAIN_DAYS      days of history for the regression (default: 365)
  WEATHER_VARIABLES  the GEFS variables to regress on, comma separated (default:
                  temperature_2m). Names come from `python gefs_weather.py list`; add :max,
                  :min, :mean or :sum to choose the daily statistic, e.g.
                  temperature_2m,downward_short_wave_radiation_flux_surface,wind_u_10m
  S3_KEY, S3_SECRET  a key that can read the challenge's bucket. Set both when the bucket is
                  private (ask the challenge for a read-only key); the targets are then
                  downloaded with the key instead of by URL (this uses boto3).
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

import gefs_weather as gefs
from challenge_s3 import read_csv

PROJECT_ID = "templatecast"          # the challenge's project_id
THEME = "lakes"                      # prefix of the file name, from the challenge
MODEL_ID = "example_lake_model"      # must match challenge-model.yaml
HORIZON = 30                         # days ahead of the reference date (yesterday's run reaches 34)

# The forecast is dated today (UTC) and uses yesterday's weather run, so it has no delay.
reference_date = pd.Timestamp(os.environ.get("REFERENCE_DATE")
                              or pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d"))
init_date = reference_date - pd.Timedelta(days=1)       # the GEFS run the forecast uses
file = Path("forecasts") / f"{THEME}-{reference_date.date()}-{MODEL_ID}.csv.gz"
if file.exists() and os.environ.get("FORCE") != "1":
    print(f"{file} exists already; not making it again (FORCE=1 replaces it).")
    sys.exit(0)
train_days = int(os.environ.get("TRAIN_DAYS", "365"))
targets_url = os.environ.get("TARGETS_URL") or (
    "https://s3.us-east-005.backblazeb2.com/template-challenge/targets/"
    f"project_id={PROJECT_ID}/duration=P1D/lakes-targets.csv.gz"
)
sites_url = os.environ.get("SITES_URL") or (
    "https://raw.githubusercontent.com/rqthomas/challenge-template/main/sites.csv")
# The weather variables to download and regress on: any names `gefs_weather.py list` prints.
weather_variables = [v.strip() for v in
                     os.environ.get("WEATHER_VARIABLES", "temperature_2m").split(",")]
# The columns they come back in: "temperature_2m:max" is the column temperature_2m_max.
weather_columns = [v.replace(":", "_") for v in weather_variables]


# Targets: the observed water temperature -----------------------------------------
targets = read_csv(targets_url)   # by URL, or with S3_KEY / S3_SECRET for a private bucket
targets = targets[(targets["variable"] == "temperature") & targets["observation"].notna()].copy()
targets["date"] = pd.to_datetime(targets["datetime"], utc=True).dt.tz_localize(None).dt.normalize()
targets = targets[targets["date"] <= reference_date]
targets = targets[["site_id", "date", "observation"]].rename(
    columns={"observation": "water_temperature"})
last_observed = targets["date"].max()
print(f"Targets: {len(targets)} observations at {targets['site_id'].nunique()} lakes, "
      f"the latest on {last_observed.date()}")

# Weather: NOAA GEFS from dynamical.org -------------------------------------------
# The lakes' coordinates come from the challenge's site list.
sites = pd.read_csv(sites_url)[["site_id", "latitude", "longitude"]]

# The weather each lake had over the past year (the regression's input), from the GEFS
# analysis ...
air_history = gefs.history(sites, last_observed, train_days, weather_variables)
air_history["date"] = pd.to_datetime(air_history["date"])
# ... and the weather yesterday's GEFS run forecasts: one value per variable, ensemble member
# and day. Only that run is used (max_back=0). It reaches HORIZON + 1 days after itself, the
# first of which is today, the reference date, so that day is dropped below.
try:
    air_forecast = gefs.forecast(sites, init_date, HORIZON + 1, weather_variables, max_back=0)
except gefs.RunNotReady as error:
    print(f"{error} No forecast for {reference_date.date()} was made; a later run will try again.")
    sys.exit(0)
air_forecast["date"] = pd.to_datetime(air_forecast["date"])
air_forecast = air_forecast[air_forecast["date"] > reference_date]   # strictly after the reference
print(f"Forecast for {reference_date.date()} from the GEFS run of {init_date.date()}: "
      f"{air_forecast['ensemble_member'].nunique()} members, {air_forecast['date'].nunique()} days")

# Forecast ----------------------------------------------------------------------
# One regression per lake: water temperature on the weather variables (ordinary least
# squares with an intercept).
training = targets.merge(air_history, on=["site_id", "date"]).dropna(
    subset=["water_temperature"] + weather_columns)


def design(frame):
    return np.column_stack([np.ones(len(frame))] + [frame[c].to_numpy() for c in weather_columns])


models = {}
for site, d in training.groupby("site_id"):
    X, y = design(d), d["water_temperature"].to_numpy()
    beta = np.linalg.lstsq(X, y, rcond=None)[0]
    residuals = y - X @ beta
    sigma = np.sqrt(residuals @ residuals / (len(y) - X.shape[1]))   # residual standard error
    r2 = 1 - residuals @ residuals / ((y - y.mean()) @ (y - y.mean()))
    models[site] = (beta, sigma)
    terms = ", ".join(f"{name} = {b:.3f}" for name, b in zip(["(Intercept)"] + weather_columns, beta))
    print(f"{site}: {terms}  (R2 {r2:.2f}, residual sd {sigma:.2f}, {len(y)} days)")

rng = np.random.default_rng((reference_date - pd.Timestamp(0)).days)   # same forecast each time
forecast = air_forecast.copy()
for site, (beta, sigma) in models.items():
    rows = forecast["site_id"] == site
    # Each member's weather through the regression, plus a draw from the residuals.
    forecast.loc[rows, "prediction"] = (
        design(forecast[rows]) @ beta + rng.normal(0, sigma, rows.sum()))

# Standard format ---------------------------------------------------------------
# One row per site, date and ensemble member (the member number is the `parameter`).
# See the challenge's "How to forecast" page for the full description of the columns.
forecast = pd.DataFrame({
    "project_id": PROJECT_ID,   # optional label: leave this column out to share the file with other challenges
    "model_id": MODEL_ID,
    "reference_datetime": reference_date.strftime("%Y-%m-%d"),
    "datetime": forecast["date"].dt.strftime("%Y-%m-%d"),
    "duration": "P1D",
    "site_id": forecast["site_id"],
    "variable": "temperature",
    "family": "ensemble",
    "parameter": forecast["ensemble_member"],
    "prediction": forecast["prediction"],
})

# Write the file ----------------------------------------------------------------
# <theme>-<reference date>-<model_id>.csv.gz, in the folder challenge-model.yaml names.
Path("forecasts").mkdir(exist_ok=True)
forecast.to_csv(file, index=False)                 # the .gz extension makes it gzip
print(f"Wrote {file} ({len(forecast)} rows)", file=sys.stderr)
