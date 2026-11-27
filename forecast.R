# Example forecast for the template challenge ("templatecast"): daily water
# temperature at two reservoirs, up to 30 days ahead, as a 31-member ensemble.
#
# The model is a regression of water temperature on air temperature, driven by the NOAA
# GEFS weather ensemble from dynamical.org:
#
#   1. Weather. gefs_weather.py reads daily weather at each lake (by default the 2 m air
#      temperature, but any of GEFS's 27 variables: see WEATHER_VARIABLES below) from
#      dynamical.org (https://dynamical.org), whose datasets anyone can read over HTTPS
#      with no account. For the weather the lakes had, it uses the "NOAA GEFS analysis" (the
#      model's best estimate of past weather, 3-hourly since 2000); for the weather to
#      come, the 31 ensemble members of the "NOAA GEFS forecast, 35 day" run of the day
#      before. Both are the same GEFS model, so a relationship fitted to one carries over.
#   2. Fit. At each lake, a linear regression of the observed water temperature on that
#      weather (lm(water ~ air temperature)) over the past year.
#   3. Forecast. Each GEFS member's weather goes through the regression, plus a random
#      draw from the regression's residuals, to make one member of the water temperature
#      forecast. The 31 members are submitted as an ensemble: the weather uncertainty comes
#      from GEFS and the model's own uncertainty from the residuals.
#
# No delay. The forecast is dated today (UTC) and uses yesterday's GEFS run, so it can be
# submitted the day it is made. GEFS runs 35 days, so yesterday's run reaches 34 days after
# today, more than the 30 the forecast needs; the day it shares with today is dropped. If yesterday's run is not complete yet (it is
# added to the dataset in one update a day, at about 06:45 UTC: run this after about 07:00 UTC)
# no file is made and the script ends
# quietly; a later run makes it. A file that already exists is never overwritten, because
# the challenge ignores changes to a file it has read.
#
# Replace the "Forecast" section with your own model; keep the rest, which reads the
# targets and the weather and writes the file in the format the challenge expects.
#
# Run from the repository root:  Rscript forecast.R
# Needs the R packages readr and dplyr, and Python with the packages in
# requirements.txt (pip install -r requirements.txt) for the weather.
#
# Environment variables (all optional)
#   REFERENCE_DATE  the day the forecast starts from, YYYY-MM-DD (default: today, UTC). The
#                   weather is the GEFS run of the day before; the file is named for this date.
#   FORCE           set to 1 to replace a file that already exists (it is kept by default)
#   TARGETS_URL     where to read the challenge's targets (default: the published file)
#   SITES_URL       the challenge's site list, with each lake's latitude and longitude
#   PYTHON          the Python to run (default: python3)
#   WEATHER_VARIABLES  the GEFS variables to regress on, comma separated (default:
#                   temperature_2m). Names come from `python3 gefs_weather.py list`; add
#                   :max, :min, :mean or :sum to choose the daily statistic, e.g.
#                   temperature_2m,downward_short_wave_radiation_flux_surface,wind_u_10m
#   TRAIN_DAYS      days of history for the regression (default: 365)
#   S3_KEY, S3_SECRET  a key that can read the challenge's bucket. Set both when the
#                   bucket is private (ask the challenge for a read-only key); the
#                   targets are then downloaded with the key instead of by URL.
#                   This needs the paws.storage package.

library(readr)
library(dplyr)

project_id <- "templatecast"          # the challenge's project_id
theme <- "lakes"                      # prefix of the file name, from the challenge
model_id <- "example_lake_model"      # must match challenge-model.yaml
horizon <- 30                         # days ahead of the reference date (yesterday's run reaches 34)

# The forecast is dated today (UTC) and uses yesterday's weather run, so it has no delay.
reference_date <- as.Date(Sys.getenv("REFERENCE_DATE", format(Sys.time(), "%Y-%m-%d", tz = "UTC")))
init_date <- reference_date - 1       # the GEFS run the forecast uses
file <- file.path("forecasts", paste0(theme, "-", reference_date, "-", model_id, ".csv.gz"))
if (file.exists(file) && Sys.getenv("FORCE") != "1") {
  message(file, " exists already; not making it again (FORCE=1 replaces it).")
  quit(save = "no", status = 0)
}
train_days <- as.integer(Sys.getenv("TRAIN_DAYS", "365"))
python <- Sys.getenv("PYTHON", "python3")
# The weather variables to download and regress on: any names `gefs_weather.py list` prints.
weather_variables <- trimws(strsplit(Sys.getenv("WEATHER_VARIABLES", "temperature_2m"), ",")[[1]])
# The columns they come back in: "temperature_2m:max" is the column temperature_2m_max.
weather_columns <- sub(":", "_", weather_variables, fixed = TRUE)
targets_url <- Sys.getenv("TARGETS_URL", paste0(
  "https://s3.us-east-005.backblazeb2.com/template-challenge/targets/",
  "project_id=", project_id, "/duration=P1D/lakes-targets.csv.gz"
))
sites_url <- Sys.getenv("SITES_URL",
  "https://raw.githubusercontent.com/rqthomas/challenge-template/main/sites.csv")

# Reading the targets -----------------------------------------------------------
# A public bucket is read by its URL. With S3_KEY and S3_SECRET set, the same
# address is read through the S3 API with the key, which also works when the bucket
# is private: the file is downloaded to a temporary folder first. (paws.storage is
# the S3 client; it is plain R, so there is nothing else to install.)
read_targets <- function(url) {
  key <- Sys.getenv("S3_KEY")
  secret <- Sys.getenv("S3_SECRET")
  if (!grepl("^https?://", url) || !nzchar(key) || !nzchar(secret)) {
    return(read_csv(url, show_col_types = FALSE, progress = FALSE))
  }
  # https://<endpoint>/<bucket>/<object>
  parts <- regmatches(url, regexec("^(https?://[^/]+)/([^/]+)/(.+)$", url))[[1]]
  endpoint <- parts[2]
  # Backblaze B2 endpoints name their region: s3.<region>.backblazeb2.com
  region <- sub("^https?://s3\\.([a-z0-9-]+)\\.backblazeb2\\.com$", "\\1", endpoint)
  if (identical(region, endpoint)) region <- "us-east-1"
  config <- list(
    credentials = list(creds = list(access_key_id = key, secret_access_key = secret)),
    endpoint = endpoint, region = region, s3_force_path_style = TRUE
  )
  # Newer paws sends a checksum with every request; ask it not to, which some
  # S3-compatible stores need (older paws does not have the setting).
  s3 <- tryCatch(
    paws.storage::s3(config = c(config, list(request_checksum_calculation = "when_required"))),
    error = function(e) paws.storage::s3(config = config)
  )
  body <- s3$get_object(Bucket = parts[3], Key = parts[4])$Body
  local <- tempfile(fileext = if (grepl("\\.gz$", parts[4])) ".csv.gz" else ".csv")
  on.exit(unlink(local))
  writeBin(body, local)
  read_csv(local, show_col_types = FALSE, progress = FALSE)
}

# Targets: the observed water temperature -----------------------------------------
targets <- read_targets(targets_url) |>
  filter(variable == "temperature", !is.na(observation),
         as.Date(datetime) <= reference_date) |>
  mutate(date = as.Date(datetime)) |>
  select(site_id, date, water_temperature = observation)
last_observed <- max(targets$date)
message("Targets: ", nrow(targets), " observations at ", n_distinct(targets$site_id),
        " lakes, the latest on ", last_observed)

# Weather: NOAA GEFS from dynamical.org -------------------------------------------
# The lakes' coordinates come from the challenge's site list.
sites_file <- tempfile(fileext = ".csv")
download.file(sites_url, sites_file, quiet = TRUE)

# Runs gefs_weather.py for the chosen variables and reads back the CSV it writes.
gefs <- function(mode, ...) {
  out <- tempfile(fileext = ".csv")
  status <- system2(python, c("gefs_weather.py", mode, sites_file, out, ...,
                              "--variables", weather_variables))
  # 75: the run asked for is not complete in the dataset yet (see gefs_weather.py).
  if (identical(as.integer(status), 75L)) {
    message("No forecast for ", reference_date, " was made; a later run will try again.")
    quit(save = "no", status = 0)
  }
  if (status != 0 || !file.exists(out)) {
    stop("gefs_weather.py failed. Is Python set up? Try: pip install -r requirements.txt",
         call. = FALSE)
  }
  read_csv(out, show_col_types = FALSE, progress = FALSE)
}

# The weather each lake had over the past year (the regression's input), from the GEFS analysis ...
air_history <- gefs("history", "--date", format(last_observed), "--days", train_days)
# ... and the weather yesterday's GEFS run forecasts: one value per variable, ensemble member
# and day. Only that run is used (--max-back 0). It reaches horizon + 1 days after itself, the
# first of which is today, the reference date, so that day is dropped.
air_forecast <- gefs("forecast", "--date", format(init_date), "--horizon", horizon + 1,
                     "--max-back", 0) |>
  filter(as.Date(date) > reference_date)
message("Forecast for ", reference_date, " from the GEFS run of ", init_date, ": ",
        n_distinct(air_forecast$ensemble_member), " members, ",
        n_distinct(air_forecast$date), " days")

# Forecast ----------------------------------------------------------------------
# One regression per lake: water temperature on the weather variables.
training <- inner_join(targets, air_history |> mutate(date = as.Date(date)),
                       by = c("site_id", "date"))
models <- training |>
  group_by(site_id) |>
  group_map(function(d, key) {
    fit <- lm(reformulate(weather_columns, response = "water_temperature"), data = d)
    message(key$site_id, ": ", paste(names(coef(fit)), round(coef(fit), 3), sep = " = ",
                                     collapse = ", "),
            "  (R2 ", round(summary(fit)$r.squared, 2), ", residual sd ", round(sigma(fit), 2),
            ", ", nrow(d), " days)")
    list(site_id = key$site_id, fit = fit, sigma = sigma(fit))
  }) |>
  (\(x) setNames(x, vapply(x, `[[`, "", "site_id")))()

set.seed(as.integer(reference_date))   # the same forecast every time it is made for a day
forecast <- air_forecast |>
  group_by(site_id) |>
  group_modify(function(d, key) {
    m <- models[[key$site_id]]
    # Each member's weather through the regression, plus a draw from the residuals.
    d$prediction <- predict(m$fit, newdata = d) + rnorm(nrow(d), 0, m$sigma)
    d
  }) |>
  ungroup()

# Standard format ---------------------------------------------------------------
# One row per site, date and ensemble member (the member number is the `parameter`).
# See the challenge's "How to forecast" page for the full description of the columns.
forecast <- forecast |>
  transmute(
    project_id = project_id,   # optional label: leave this column out to share the file with other challenges
    model_id = model_id,
    reference_datetime = reference_date,
    datetime = as.Date(date),
    duration = "P1D",
    site_id,
    variable = "temperature",
    family = "ensemble",
    parameter = ensemble_member,
    prediction
  )

# Write the file ----------------------------------------------------------------
# <theme>-<reference date>-<model_id>.csv.gz, in the folder challenge-model.yaml names.
dir.create("forecasts", showWarnings = FALSE)
write_csv(forecast, file)
message("Wrote ", file, " (", nrow(forecast), " rows)")
