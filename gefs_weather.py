#!/usr/bin/env python3
"""Daily weather at points, from the NOAA GEFS datasets on dynamical.org: any variables you choose.

dynamical.org publishes weather datasets as Icechunk repositories (cloud-optimized Zarr)
that anyone can read over HTTPS, with no account. This reads two of its NCEP Global Ensemble
Forecast System (GEFS) datasets at the grid cell nearest each site:

  forecast  "NOAA GEFS forecast, 35 day": 31 ensemble members (0-30) initialized every day at
            00 UTC since 2020-10-01, 3-hourly steps for 10 days then 6-hourly, to 35 days.
            https://dynamical.org/catalog/noaa-gefs-forecast-35-day/
  analysis  "NOAA GEFS analysis": the model's best estimate of past weather, made by joining
            the first hours of each historical forecast into one 3-hourly series since 2000.
            It is the same model, so a relationship fitted to it carries over to the forecast.
            https://dynamical.org/catalog/noaa-gefs-analysis/

Data: NOAA NWS NCEP GEFS, processed by dynamical.org (CC BY 4.0).

Usage (every mode writes a CSV; --variables takes any of the names `list` prints):

  gefs_weather.py list
      The 27 variables: name, units, how each step is defined, description.

  gefs_weather.py history SITES OUT --date 2026-10-31 --days 365 \\
        --variables temperature_2m relative_humidity_2m
      The weather each day for the `days` days up to --date, from the analysis.
      Columns: site_id, date, then one column per variable.

  gefs_weather.py forecast SITES OUT --date 2026-11-20 --horizon 30 \\
        --variables temperature_2m relative_humidity_2m
      The ensemble forecast, one row per member and day for the days after the run.
      Columns: site_id, init_date, ensemble_member, date, then one column per variable.
      If the run of --date is not complete yet, an earlier one is used (up to --max-back days
      before, default 3); with --max-back 0 only that run is used, and the command exits with
      status 75 ("try again later") when it is not complete.

SITES is a CSV with site_id, latitude and longitude columns.

Daily values: the 3-hourly (or 6-hourly) steps of each UTC day are averaged, except that a
variable whose steps are a maximum (maximum_temperature_2m) or minimum
(minimum_temperature_2m) over the step uses max or min. To choose, add a statistic to a
name: temperature_2m:max, temperature_2m:min, relative_humidity_2m:min (mean, min, max or
sum); the column is then named temperature_2m_max and so on. Values keep the dataset's
units (see `list`). Note that precipitation_surface is a rate in kg m-2 s-1, which is mm per
second: its daily mean times 86400 is the day's total in mm (summing the steps would not be).

Only the grid cells you ask for are downloaded, a few megabytes. Needs:
  pip install icechunk xarray zarr pystac numpy pandas        (see requirements.txt)
"""
import argparse
import sys

import numpy as np
import pandas as pd
import pystac
import xarray as xr
import icechunk

STAC_CATALOG = "https://stac.dynamical.org/catalog.json"
FORECAST = "noaa-gefs-forecast-35-day"   # init_time x ensemble_member x lead_time x lat x lon
NOT_READY = 75   # exit status of the command line when the run asked for is not complete
ANALYSIS = "noaa-gefs-analysis"          # time x lat x lon, 3-hourly
STATS = ("mean", "min", "max", "sum")
STEPS_PER_DAY_ANALYSIS = 8               # 3-hourly


def open_dataset(name):
    """Open a dynamical.org dataset lazily. Nothing is downloaded until values are loaded."""
    catalog = pystac.Catalog.from_file(STAC_CATALOG)
    asset = catalog.get_child(name).assets["icechunk-https"]
    repo = icechunk.Repository.open(icechunk.http_storage(asset.href))
    session = repo.readonly_session("main")
    return xr.open_zarr(session.store, chunks=None)


def parse_variables(specs, ds):
    """Turn `name` or `name:stat` into (column, name, stat), checking the names exist."""
    parsed = []
    for spec in specs:
        name, _, stat = spec.partition(":")
        if name not in ds.data_vars:
            close = [v for v in ds.data_vars if name.split("_")[0] in v][:5]
            hint = f" Did you mean one of: {', '.join(close)}?" if close else ""
            sys.exit(f"'{name}' is not a GEFS variable.{hint} Run `gefs_weather.py list` to see them all.")
        if stat and stat not in STATS:
            sys.exit(f"'{spec}': the statistic must be one of {', '.join(STATS)}.")
        step_type = ds[name].attrs.get("step_type")
        default = step_type if step_type in ("min", "max") else "mean"
        parsed.append((f"{name}_{stat}" if stat else name, name, stat or default))
    if len({column for column, _, _ in parsed}) != len(parsed):
        sys.exit("The same variable and statistic was given twice.")
    return parsed


def point(ds, site):
    """The series at the grid cell nearest a site (longitudes run -180 to 179.75)."""
    return ds.sel(latitude=site.latitude, longitude=site.longitude, method="nearest")


def daily(frame, key, parsed):
    """Aggregate steps to UTC days: `frame` has a `date` column, the variables and `key` columns."""
    # Named aggregation, so one variable can appear twice with different statistics.
    named = {column: pd.NamedAgg(column=name, aggfunc=stat) for column, name, stat in parsed}
    return frame.groupby(key + ["date"]).agg(**named)


def history(sites, end, days, parsed_specs):
    """Daily weather for the `days` days up to `end`, from the GEFS analysis."""
    analysis = open_dataset(ANALYSIS)
    parsed = parse_variables(parsed_specs, analysis)
    names = sorted({name for _, name, _ in parsed})
    start = end - pd.Timedelta(days=days - 1)
    rows = []
    for site in sites.itertuples():
        # Whole days only: the steps from 00 to 21 UTC of `start` .. `end`.
        data = point(analysis[names], site).sel(
            time=slice(start, end + pd.Timedelta(hours=21))).load()
        frame = data.to_dataframe()[names]
        frame["date"] = frame.index.normalize()
        full = frame.groupby("date").size() == STEPS_PER_DAY_ANALYSIS
        table = daily(frame, [], parsed)
        table = table[full.reindex(table.index).fillna(False).values].reset_index()
        table["date"] = table["date"].dt.strftime("%Y-%m-%d")
        table.insert(0, "site_id", site.site_id)
        rows.append(table)
    return pd.concat(rows, ignore_index=True)


class RunNotReady(Exception):
    """The GEFS run asked for is not complete in the dataset yet."""


def forecast(sites, init_date, horizon, parsed_specs, max_back=3):
    """Daily weather for each ensemble member, for `horizon` days after the run.

    The dataset is updated once a day, at about 06:45 UTC. That update adds the first part of
    the same day's run (about 13 days) and completes the run of the day before, so a run
    is complete from about 06:45 UTC the next day. It is complete only when its last lead
    time has data. With the default `max_back` a run that is not
    complete is skipped for the one before it, up to `max_back` days earlier. With
    `max_back=0` only the run of `init_date` is used, and RunNotReady is raised if it is
    not complete (the caller can try again later).
    """
    ds = open_dataset(FORECAST)
    parsed = parse_variables(parsed_specs, ds)
    names = sorted({name for _, name, _ in parsed})
    first = sites.iloc[0]
    for back in range(max_back + 1):
        init = init_date - pd.Timedelta(days=back)
        if init not in ds["init_time"].values:
            continue
        last_step = point(ds[names[0]], first).sel(init_time=init).isel(lead_time=-1)
        if not np.isnan(last_step.load()).any():
            break
    else:
        if max_back == 0:
            raise RunNotReady(f"The GEFS run of {init_date.date()} is not complete yet.")
        raise RunNotReady(f"No complete GEFS run found on or before {init_date.date()}.")
    print(f"Using the GEFS run initialized {init} ({back} day(s) before {init_date.date()})",
          file=sys.stderr)

    rows = []
    for site in sites.itertuples():
        run = point(ds[names], site).sel(init_time=init).load()
        frame = run.to_dataframe()[names].reset_index()
        # Each step's valid time is the run's start plus its lead time; group the steps by day.
        frame["date"] = (init + frame["lead_time"]).dt.normalize()
        table = daily(frame, ["ensemble_member"], parsed).reset_index()
        table = table[(table["date"] > init) & (table["date"] <= init + pd.Timedelta(days=horizon))]
        table["date"] = table["date"].dt.strftime("%Y-%m-%d")
        table.insert(0, "site_id", site.site_id)
        table.insert(1, "init_date", init.strftime("%Y-%m-%d"))
        rows.append(table.dropna())
    columns = ["site_id", "init_date", "ensemble_member", "date"] + [c for c, _, _ in parsed]
    return pd.concat(rows, ignore_index=True)[columns]


def list_variables():
    """Print the variables, from the dataset's own metadata (no data is downloaded)."""
    ds = open_dataset(FORECAST)
    print(f"{'variable':45s} {'units':15s} {'step':8s} description")
    for name in sorted(ds.data_vars):
        a = ds[name].attrs
        print(f"{name:45s} {str(a.get('units')):15s} {str(a.get('step_type')):8s} {a.get('long_name')}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("mode", choices=["list", "history", "forecast"])
    parser.add_argument("sites", nargs="?", help="CSV with site_id, latitude and longitude columns")
    parser.add_argument("out", nargs="?", help="CSV to write")
    parser.add_argument("--date", help="last day (history) or run date (forecast), YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=365, help="history: how many days back")
    parser.add_argument("--horizon", type=int, default=30, help="forecast: days ahead")
    parser.add_argument("--max-back", type=int, default=3,
                        help="forecast: use an earlier run, up to this many days before --date, "
                             "if the run of --date is not complete (0: only that run)")
    parser.add_argument("--variables", nargs="+", default=["temperature_2m"],
                        help="variable names from `list`, optionally with :mean/:min/:max/:sum")
    args = parser.parse_args()

    if args.mode == "list":
        return list_variables()
    if not (args.sites and args.out and args.date):
        parser.error(f"{args.mode} needs SITES, OUT and --date")
    sites = pd.read_csv(args.sites)[["site_id", "latitude", "longitude"]]
    date = pd.Timestamp(args.date)
    if args.mode == "history":
        result = history(sites, date, args.days, args.variables)
    else:
        try:
            result = forecast(sites, date, args.horizon, args.variables, args.max_back)
        except RunNotReady as error:
            # 75 is "temporary failure, try again later" (sysexits.h); forecast.R looks for it.
            print(error, file=sys.stderr)
            sys.exit(NOT_READY)
    result.to_csv(args.out, index=False)
    print(f"Wrote {len(result)} rows to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
