# Monitor client for R: report a job's progress to the Monitor dashboard.
# The interface every language's client shares is in clients/README.md.
#
#   source("~/projects/monitor/clients/R/monitor_client.R")
#
#   run <- monitor_panel("laptop-calibration")      # group "laptop", from the id
#   run$stage("loading data")
#   for (i in seq_along(regions)) {
#     fit(regions[i])
#     run$progress(i / length(regions))
#   }
#   run$done()
#
# Optional extras: monitor_panel(id, name =, group =, priority = TRUE, stale_after = "2h"),
# run$progress(f, stage = "region 3", n_results = 3) (other named arguments become stats), run$warn(),
# run$error(), run$track("writing outputs", write_outputs()).
#
# Uses the login saved by `monitor-agent login URL TOKEN` (~/.config/monitor/client.json), or the
# MONITOR_URL / MONITOR_TOKEN environment variables. Sending needs the `curl` and `jsonlite` packages.
# Without a login, or with MONITOR_URL=off, nothing is sent and progress is printed instead (see
# monitor_lite.R, the copy-anywhere version of that). Interactively, progress is printed as well as sent.
#
# In Rscript/cron, an uncaught error turns the job's panels red (catch_errors = FALSE to leave R's error
# handling alone). Calls are cheap enough for tight loops. Routine updates (progress, stage, stats) are
# combined and sent at most every `min_interval` seconds (default 5); a status change, an error,
# completion or a panel's first update go out at once. Sends don't wait for the server: they finish
# during later calls. done(), error() and the end of the R session wait (up to 5 s) until everything is
# sent. Network problems give a warning and are otherwise ignored: monitoring never stops your script.

monitor_connect <- function(...) NULL   # defined properly below; marks the full client as loaded

# The console output (monitor_lite.R) lives next to this file.
.monitor_client_dir <- local({
  f <- NULL
  for (i in rev(seq_len(sys.nframe()))) {
    o <- tryCatch(sys.frame(i)$ofile, error = function(e) NULL)
    if (is.character(o)) { f <- o; break }
  }
  if (is.null(f) && nzchar(Sys.getenv("MONITOR_R_CLIENT"))) f <- path.expand(Sys.getenv("MONITOR_R_CLIENT"))
  if (is.null(f)) NULL else dirname(normalizePath(f))
})
if (!is.null(.monitor_client_dir) && file.exists(file.path(.monitor_client_dir, "monitor_lite.R"))) {
  source(file.path(.monitor_client_dir, "monitor_lite.R"), local = environment())
}
if (!exists(".monitor_add_methods", mode = "function")) {
  stop("monitor_client.R needs monitor_lite.R from the same folder (couldn't find it)", call. = FALSE)
}

.monitor_status_aliases <- c(
  green = "green", ok = "green", good = "green", up = "green", running = "green", done = "green",
  yellow = "yellow", checking = "yellow", warn = "yellow", warning = "yellow", pending = "yellow",
  red = "red", error = "red", stopped = "red", down = "red", failed = "red", fail = "red",
  grey = "grey", gray = "grey", unknown = "grey")
.monitor_state_fields <- c("status", "stage", "error", "progress", "stats")

#' Connection settings: explicit arguments, then environment variables, then the saved login.
#' With no server (or "off"), the connection isn't connected: nothing is sent, panels print instead.
#' min_interval: seconds between routine updates per panel (env MONITOR_MIN_INTERVAL, default 5).
#' echo: also print progress while sending (default: in interactive sessions and on a terminal).
monitor_connect <- function(url = NULL, token = NULL, timeout = 10, quiet = FALSE, min_interval = NULL,
                            echo = NULL) {
  pick <- function(...) {
    for (v in list(...)) if (!is.null(v) && length(v) == 1 && !is.na(v) && nzchar(v)) return(v)
    NULL
  }
  saved <- list()
  cfg <- Sys.getenv("MONITOR_CLIENT_CONFIG", file.path(path.expand("~"), ".config", "monitor", "client.json"))
  url <- pick(url, Sys.getenv("MONITOR_URL"))
  if ((is.null(url) || is.null(pick(token, Sys.getenv("MONITOR_TOKEN")))) && file.exists(cfg)) {
    saved <- if (requireNamespace("jsonlite", quietly = TRUE)) {
      tryCatch(jsonlite::fromJSON(cfg), error = function(e) list())
    } else list(url = "needs-jsonlite")   # logged in, but can't send without the packages: error below
  }
  url <- pick(url, saved$url)
  connected <- !is.null(url) && !(tolower(url) %in% c("off", "none", "0", "false"))
  if (connected) {
    for (pkg in c("curl", "jsonlite")) {
      if (!requireNamespace(pkg, quietly = TRUE)) {
        stop("monitor_client.R needs the '", pkg, "' package to send updates: install.packages(\"", pkg,
             "\")  (or set MONITOR_URL=off to only print progress)", call. = FALSE)
      }
    }
  }
  if (is.null(echo)) echo <- interactive() || isatty(stderr())
  if (!connected) echo <- TRUE
  if (is.null(min_interval)) min_interval <- as.numeric(pick(Sys.getenv("MONITOR_MIN_INTERVAL"), "5"))

  # Mutable sending state, shared by every panel using this connection.
  st <- new.env()
  st$pool <- if (connected) curl::new_pool() else NULL
  st$panels <- new.env()   # panel id -> environment: eff (pending effects), status, error, done, sent
  st$queue <- character()  # ids with pending updates, in order
  st$urgent <- character()
  st$inflight <- FALSE
  st$last_call <- NA_real_
  st$last_pump <- 0
  st$n_sent <- 0L
  mon <- structure(list(url = if (connected) sub("/+$", "", url) else NULL, connected = connected, echo = echo,
                        token = if (is.null(tk <- pick(token, Sys.getenv("MONITOR_TOKEN"), saved$token))) "" else tk,
                        timeout = timeout, quiet = quiet, min_interval = min_interval, state = st),
                   class = "monitor_connection")
  st$mon <- mon
  if (connected) reg.finalizer(st, function(e) .monitor_flush(e$mon, timeout = 5), onexit = TRUE)  # send leftovers at exit
  mon
}

.monitor_headers <- function(mon) {
  headers <- list("Content-Type" = "application/json")
  if (nzchar(mon$token)) {
    headers[["Authorization"]] <- paste("Bearer", mon$token)
    headers[["X-Token"]] <- mon$token  # some hosts (Apache CGI) strip Authorization
  }
  headers
}

.monitor_warn <- function(mon, msg) if (!mon$quiet) warning(paste("[monitor]", msg), call. = FALSE, immediate. = TRUE)

#' Synchronous request (used for reads; updates go through the queue below).
.monitor_request <- function(mon, method, path, body = NULL) {
  if (!isTRUE(mon$connected)) return(invisible(NULL))
  h <- curl::new_handle(customrequest = method, timeout = mon$timeout, failonerror = FALSE)
  do.call(curl::handle_setheaders, c(list(h), .monitor_headers(mon)))
  if (!is.null(body)) curl::handle_setopt(h, postfields = .monitor_json(body))
  tryCatch({
    r <- curl::curl_fetch_memory(paste0(mon$url, path), handle = h)
    parsed <- tryCatch(jsonlite::fromJSON(rawToChar(r$content), simplifyVector = FALSE), error = function(e) NULL)
    if (r$status_code >= 400) {
      .monitor_warn(mon, sprintf("%s %s failed: %s", method, path,
                                 if (is.list(parsed) && !is.null(parsed$detail)) parsed$detail else paste("HTTP", r$status_code)))
      return(invisible(NULL))
    }
    invisible(parsed)
  }, error = function(e) {
    .monitor_warn(mon, sprintf("%s %s failed: %s", method, path, conditionMessage(e)))
    invisible(NULL)
  })
}

.monitor_json <- function(body) {
  if (!is.null(body$clear)) body$clear <- I(as.character(unlist(body$clear)))  # always a JSON array
  jsonlite::toJSON(body, auto_unbox = TRUE, null = "null", na = "null", digits = NA)
}

# ---- combining updates -----------------------------------------------------------------------
# An update's effect per field: list(op = "set", v) / list(op = "clear") for plain fields and
# list(op = "merge" | "replace", v) for stats. The server applies `clear` after the other fields.
.monitor_effects <- function(u, eff = list()) {
  if (is.null(u$clear) && is.null(u$replace_stats)) {  # fast path: the common case in loops
    for (k in names(u)) {
      if (k == "stats") {
        if (is.null(eff$stats)) eff$stats <- list(op = "merge", v = u$stats)
        else eff$stats$v[names(u$stats)] <- u$stats
      } else {
        eff[[k]] <- list(op = "set", v = u[[k]])
      }
    }
    return(eff)
  }
  clear <- as.character(unlist(u$clear))
  for (k in setdiff(names(u), c("clear", "replace_stats"))) {
    if (k %in% clear && k %in% .monitor_state_fields) next
    if (k == "stats") {
      if (isTRUE(u$replace_stats) || is.null(eff$stats)) {
        eff$stats <- list(op = if (isTRUE(u$replace_stats)) "replace" else "merge", v = u$stats)
      } else {
        eff$stats$v[names(u$stats)] <- u$stats   # merge onto earlier stats
      }
    } else {
      eff[[k]] <- list(op = "set", v = u[[k]])
    }
  }
  for (k in intersect(clear, .monitor_state_fields)) {
    eff[[k]] <- if (k == "stats") list(op = "replace", v = list()) else list(op = "clear")
  }
  eff
}

.monitor_encode <- function(eff) {
  body <- list()
  clear <- character()
  for (k in names(eff)) {
    e <- eff[[k]]
    if (e$op == "set") {
      body[[k]] <- e$v
    } else if (e$op == "clear" || (e$op == "replace" && length(e$v) == 0)) {
      clear <- c(clear, k)
    } else if (length(e$v)) {
      body[[k]] <- e$v
      if (e$op == "replace") body$replace_stats <- TRUE
    }
  }
  if (length(clear)) body$clear <- sort(clear)
  body
}

#' Combine two updates into one with the same effect as sending them in order.
.monitor_merge <- function(old, new) .monitor_encode(.monitor_effects(new, .monitor_effects(old)))

# ---- the send queue --------------------------------------------------------------------------
.monitor_submit <- function(mon, id, body) {
  if (!isTRUE(mon$connected)) return(invisible(NULL))
  st <- mon$state
  now <- as.numeric(Sys.time())
  if (!is.null(body$status) && is.na(match(body$status, c("green", "yellow", "red", "grey")))) {
    s <- .monitor_status_aliases[tolower(body$status)]
    if (!is.na(s)) body$status <- unname(s)
  }
  p <- st$panels[[id]]
  if (is.null(p)) {
    p <- new.env()
    p$status <- NULL; p$error <- NULL; p$done <- FALSE; p$sent <- -Inf; p$eff <- NULL
    p$hold_until <- -Inf; p$held <- FALSE; p$h_stage <- NULL; p$h_progress <- NULL; p$h_stats <- list()
    assign(id, p, envir = st$panels)
    urgent <- TRUE
  } else {
    urgent <- (!is.null(body$status) && !identical(body$status, p$status)) ||
      (!is.null(body$error) && !identical(body$error, p$error)) ||
      (identical(body$progress, 1) && !p$done)
  }
  .monitor_fold(st, id, p)
  clear <- body$clear
  if (!is.null(body$status)) p$status <- body$status
  if (!is.null(body$error) || "error" %in% clear) p$error <- body$error
  if (!is.null(body$progress) || "progress" %in% clear) p$done <- identical(body$progress, 1)
  if (is.null(p$eff)) {
    p$eff <- .monitor_effects(body)
    st$queue <- c(st$queue, id)
  } else {
    p$eff <- .monitor_effects(body, p$eff)
  }
  if (urgent && !(id %in% st$urgent)) st$urgent <- c(st$urgent, id)

  # If the loop is slow (seconds between calls), wait a little for sends to finish so the dashboard
  # isn't a whole iteration behind; never more than ~10% of the time between calls.
  gap <- if (is.na(st$last_call)) 0 else now - st$last_call
  st$last_call <- now
  if (urgent || (st$inflight && now - st$last_pump >= 0.05) ||
      (!st$inflight && now - p$sent >= mon$min_interval)) {
    st$last_pump <- now
    # an important update (first, status change, error) waits up to 2 s to arrive: R has no thread to
    # finish it later, and the next call may be a long computation away
    .monitor_pump(mon, wait = if (urgent) 2 else min(2, 0.1 * gap))
  }
  # until then, routine updates for this panel can just be noted (see .monitor_hold)
  p$hold_until <- if (st$inflight) now + 0.05 else p$sent + mon$min_interval
}

#' The cheap path for routine updates (same status, no error, not finished) that aren't due yet:
#' remember the latest values and return TRUE; they're folded into the next real send.
.monitor_hold <- function(mon, id, status, stage, error, progress, clear, stats) {
  if (!isTRUE(mon$connected)) return(TRUE)
  p <- mon$state$panels[[id]]
  if (is.null(p) || !is.null(error) || !is.null(clear) || (!is.null(progress) && progress >= 1) ||
      (!is.null(status) && !identical(status, p$status))) return(FALSE)
  now <- as.numeric(Sys.time())
  if (now >= p$hold_until) return(FALSE)
  mon$state$last_call <- now
  if (!is.null(stage)) p$h_stage <- stage
  if (!is.null(progress)) p$h_progress <- progress
  if (length(stats)) p$h_stats[names(stats)] <- stats
  p$held <- TRUE
  TRUE
}

.monitor_fold <- function(st, id, p) {
  if (!p$held) return(invisible())
  b <- list()
  if (!is.null(p$h_stage)) b$stage <- p$h_stage
  if (!is.null(p$h_progress)) b$progress <- p$h_progress
  if (length(p$h_stats)) b$stats <- p$h_stats
  if (is.null(p$eff)) st$queue <- c(st$queue, id)
  p$eff <- .monitor_effects(b, if (is.null(p$eff)) list() else p$eff)
  p$held <- FALSE; p$h_stage <- NULL; p$h_progress <- NULL; p$h_stats <- list()
}

.monitor_start_next <- function(mon, force = FALSE) {
  st <- mon$state
  if (st$inflight || !length(st$queue)) return(FALSE)
  now <- as.numeric(Sys.time())
  due <- NULL
  for (i in st$queue) {
    if (force || i %in% st$urgent || now - st$panels[[i]]$sent >= mon$min_interval) { due <- i; break }
  }
  if (is.null(due)) return(FALSE)
  p <- st$panels[[due]]
  body <- .monitor_encode(p$eff)
  p$eff <- NULL
  p$sent <- now
  st$queue <- setdiff(st$queue, due)
  st$urgent <- setdiff(st$urgent, due)
  if (!length(body)) return(TRUE)
  path <- paste0("/api/panels/", due)
  h <- curl::new_handle(url = paste0(mon$url, path), customrequest = "PUT", timeout = mon$timeout,
                        postfields = .monitor_json(body))
  do.call(curl::handle_setheaders, c(list(h), .monitor_headers(mon)))
  st$inflight <- TRUE
  curl::multi_add(h, pool = st$pool,
    done = function(res) {
      st$inflight <- FALSE
      st$n_sent <- st$n_sent + 1L
      if (res$status_code >= 400) {
        detail <- tryCatch(jsonlite::fromJSON(rawToChar(res$content))$detail, error = function(e) NULL)
        .monitor_warn(mon, sprintf("PUT %s failed: %s", path, if (is.null(detail)) paste("HTTP", res$status_code) else detail))
      }
    },
    fail = function(msg) {
      st$inflight <- FALSE
      .monitor_warn(mon, sprintf("PUT %s failed: %s", path, msg))
    })
  TRUE
}

#' Advance the queue without blocking (or for up to `wait` seconds).
.monitor_pump <- function(mon, wait = 0) {
  st <- mon$state
  deadline <- as.numeric(Sys.time()) + wait
  repeat {
    if (!st$inflight) .monitor_start_next(mon)
    if (!st$inflight) return(invisible(NULL))
    left <- deadline - as.numeric(Sys.time())
    curl::multi_run(timeout = max(0, left), poll = TRUE, pool = st$pool)
    if (st$inflight && deadline - as.numeric(Sys.time()) <= 0) return(invisible(NULL))
  }
}

#' Send everything pending now (ignoring the throttle) and wait up to `timeout` seconds.
.monitor_flush <- function(mon, timeout = 5) {
  if (!isTRUE(mon$connected)) return(invisible(TRUE))
  st <- mon$state
  deadline <- as.numeric(Sys.time()) + timeout
  for (id in ls(st$panels)) .monitor_fold(st, id, st$panels[[id]])
  while (st$inflight || length(st$queue)) {
    if (!st$inflight) .monitor_start_next(mon, force = TRUE)
    left <- deadline - as.numeric(Sys.time())
    if (left <= 0) {
      .monitor_warn(mon, "some updates could not be sent in time")
      return(invisible(FALSE))
    }
    if (st$inflight) curl::multi_run(timeout = left, poll = TRUE, pool = st$pool)
  }
  invisible(TRUE)
}

# ---- panels -----------------------------------------------------------------------------------
#' A panel on the dashboard; created on its first update. Definition fields are sent only if given
#' (and are ignored for panels defined in the dashboard's feed list, which owns them). Left out, the
#' name is the id and the group is the id's first part ("laptop" for "laptop-calibration").
#' catch_errors: in Rscript/cron, an uncaught error turns this panel red (unless it already finished).
monitor_panel <- function(id, name = NULL, group = NULL, priority = NULL, stale_after = NULL, url = NULL,
                          catch_errors = TRUE, monitor = monitor_connect()) {
  definition <- Filter(Negate(is.null), list(name = name, group = group, priority = priority,
                                             stale_after = stale_after, url = url))
  if (length(definition)) .monitor_submit(monitor, id, definition)
  if (!isTRUE(monitor$connected) && is.null(getOption("monitor.noted"))) {
    options(monitor.noted = TRUE)
    message("[monitor] not logged in on this machine (no MONITOR_URL or ~/.config/monitor/client.json): ",
            "printing progress instead of sending it")
  }

  self <- list(id = id, monitor = monitor)
  state <- new.env()  # finished: done() was called; reported: the last error track() reported
  state$finished <- FALSE
  state$reported <- NULL
  echo <- if (isTRUE(monitor$echo)) .monitor_console_panel(id, name) else NULL

  #' status: "green"/"yellow"/"red" (or "ok", "warn", "error"); progress: 0..1;
  #' any other named arguments become stats on the panel, e.g. n_results = 120, loss = 0.03.
  #' Returns at once; the update is sent in the background (see the top of this file).
  self$update <- function(status = NULL, stage = NULL, error = NULL, progress = NULL, clear = NULL, ...) {
    stats <- list(...)
    if (length(stats)) {
      if (is.null(names(stats)) || any(!nzchar(names(stats)))) stop("stats must be named, e.g. n_results = 12")
      if (any(lengths(stats) != 1)) {
        stats <- lapply(stats, function(v) if (length(v) == 1) v else paste(format(v), collapse = ", "))
      }
    }
    if (!is.null(stage)) stage <- as.character(stage)
    if (!is.null(progress)) {
      progress <- max(0, min(1, as.numeric(progress)))
      state$finished <- progress >= 1
    }
    if (!is.null(echo)) echo$update(status, stage, error, progress, clear, ...)
    if (.monitor_hold(monitor, id, status, stage, error, progress, clear, stats)) return(invisible(NULL))
    body <- list()
    if (!is.null(status)) body$status <- status
    if (!is.null(stage)) body$stage <- stage
    if (!is.null(error)) body$error <- substr(as.character(error), 1, 500)
    if (!is.null(progress)) body$progress <- progress
    if (length(stats)) body$stats <- stats
    if (!is.null(clear)) body$clear <- as.character(clear)
    .monitor_submit(monitor, id, body)
    invisible(NULL)
  }

  #' For Rscript / cron: report any uncaught error on this panel before R stops (monitor_panel()
  #' calls this unless catch_errors = FALSE). Interactive sessions are left alone.
  catch <- function() .monitor_watch(self, state)
  self <- .monitor_add_methods(
    self, catch = catch,
    on_error = function(msg) state$reported <- msg,
    flush = function(timeout = 5) .monitor_flush(monitor, timeout))
  if (catch_errors) self$catch_errors()
  self
}

# One error handler for all the panels of an Rscript run: each unfinished one turns red.
.monitor_watched <- new.env()
.monitor_watched$panels <- list()
.monitor_watch <- function(panel, state) {
  if (interactive()) return(invisible())
  for (w in .monitor_watched$panels) if (identical(w$state, state)) return(invisible())
  .monitor_watched$panels[[length(.monitor_watched$panels) + 1]] <- list(panel = panel, state = state)
  options(error = function() {
    .monitor_end_line()
    msg <- .monitor_clean_error(geterrmessage())
    for (w in .monitor_watched$panels) {
      # track() already reported this error, with its step name; a finished job isn't failing
      if (!w$state$finished && !identical(msg, w$state$reported)) w$panel$error(msg)
      w$panel$flush()
    }
    quit(save = "no", status = 1, runLast = FALSE)
  })
}

# "Error in f(x) : msg\nCalls: a -> f" -> "msg"
.monitor_clean_error <- function(txt) {
  txt <- sub("(?s)\\nCalls: .*$", "", txt, perl = TRUE)
  trimws(sub("^Error(?: in .*? : |: )", "", txt, perl = TRUE))
}
