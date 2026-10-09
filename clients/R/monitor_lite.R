# monitor_lite.R: progress reporting that prints to the console, with the same calls as the Monitor
# client. One file, base R only: copy it into code you share.
#
#   source("monitor_lite.R")
#
#   run <- monitor_panel("laptop-calibration")
#   run$stage("loading data")
#   for (i in seq_along(regions)) {
#     fit(regions[i])
#     run$progress(i / length(regions))
#   }
#   run$done()
#
# To report to your dashboard instead, on your own machines, point MONITOR_R_CLIENT at the full client
# (e.g. in ~/.Renviron: MONITOR_R_CLIENT=~/projects/monitor/clients/R/monitor_client.R). This file then
# loads it, and the same code reports to the dashboard there and prints progress everywhere else.
#
# Output goes to stderr: interactively or on a terminal, a progress bar redrawn in place; in a log
# file, a line per stage and every 10%.

.monitor_console <- new.env()   # which panel's progress bar is on the current line, if any
.monitor_console$open <- NULL

.monitor_end_line <- function() {
  if (!is.null(.monitor_console$open)) {
    cat("\n", file = stderr())
    .monitor_console$open <- NULL
  }
}

.monitor_fmt_time <- function(sec) {
  sec <- as.integer(max(0, sec))
  if (sec < 60) return(paste0(sec, "s"))
  if (sec < 3600) return(sprintf("%dm %02ds", sec %/% 60, sec %% 60))
  sprintf("%dh %02dm", sec %/% 3600, sec %% 3600 %/% 60)
}

#' A panel that prints its updates. Takes everything the full client's monitor_panel() does.
.monitor_console_panel <- function(id, name = NULL, ...) {
  label <- if (is.null(name)) id else name
  tty <- interactive() || isatty(stderr())
  st <- new.env()
  st$t0 <- proc.time()[["elapsed"]]
  st$stage <- NULL; st$progress <- NULL; st$stats <- list(); st$drawn <- -Inf; st$decile <- -1L
  key <- paste0(id, "#", format(st$t0, digits = 15), "#", sample.int(1e9, 1))
  elapsed <- function() proc.time()[["elapsed"]] - st$t0

  out_line <- function(text) {
    .monitor_end_line()
    cat("[", label, "] ", text, "\n", sep = "", file = stderr())
  }
  stats_text <- function() {
    s <- st$stats
    if (!length(s)) return("")
    s <- utils::head(s, 4)
    paste0(" (", paste(names(s), vapply(s, function(v) paste(format(v), collapse = ", "), ""),
                       sep = "=", collapse = ", "), ")")
  }
  bar <- function(force) {
    frac <- if (is.null(st$progress)) 0 else st$progress
    e <- elapsed()
    timing <- .monitor_fmt_time(e)
    if (frac > 0.02 && frac < 1) timing <- paste0(timing, ", ~", .monitor_fmt_time(e * (1 - frac) / frac), " left")
    stage <- if (is.null(st$stage)) "" else paste0("  ", st$stage)
    if (tty) {
      if (!force && e - st$drawn < 0.1) return(invisible())
      st$drawn <- e
      filled <- round(frac * 20)
      text <- sprintf("[%s] %3.0f%% |%s%s| %s%s%s", label, frac * 100, strrep("=", filled),
                      strrep(" ", 20 - filled), timing, stage, stats_text())
      width <- getOption("width", 80)
      if (!is.null(.monitor_console$open) && !identical(.monitor_console$open, key)) .monitor_end_line()
      cat("\r", substr(text, 1, max(20, width - 1)), strrep(" ", max(0, width - 1 - nchar(text))),
          sep = "", file = stderr())
      .monitor_console$open <- key
    } else {
      decile <- as.integer(floor(frac * 10 + 1e-9))
      if (force || decile > st$decile) {
        st$decile <- decile
        out_line(sprintf("%.0f%%, %s%s%s", frac * 100, timing, stage, stats_text()))
      }
    }
  }

  self <- list(id = id)
  self$update <- function(status = NULL, stage = NULL, error = NULL, progress = NULL, clear = NULL, ...) {
    stats <- list(...)
    if (!is.null(status)) {
      status <- tolower(status)
      if (status %in% c("error", "stopped", "down", "failed", "fail")) status <- "red"
      if (status %in% c("warn", "warning", "checking", "pending")) status <- "yellow"
    }
    if ("stage" %in% clear) st$stage <- NULL
    if ("progress" %in% clear) { st$progress <- NULL; st$decile <- -1L }
    if ("stats" %in% clear) st$stats <- list()
    if (length(stats)) st$stats[names(stats)] <- stats
    new_stage <- !is.null(stage) && !identical(stage, st$stage)
    if (!is.null(stage)) st$stage <- stage
    if (identical(status, "red")) {
      out_line(paste("ERROR:", if (!is.null(error)) error else if (!is.null(stage)) stage else "failed"))
      return(invisible(NULL))
    }
    if (!is.null(error)) out_line(paste("error:", error))
    if (!is.null(progress)) {
      st$progress <- max(0, min(1, as.numeric(progress)))
      # done() is the update that sets progress 1 and clears the error; progress(1) alone isn't
      if (st$progress >= 1 && "error" %in% clear) {
        out_line(paste0(if (is.null(stage)) "done" else stage, " after ",
                        .monitor_fmt_time(elapsed()), stats_text()))
        return(invisible(NULL))
      }
    }
    if (identical(status, "yellow") && new_stage) out_line(paste("warning:", stage))
    else if (!is.null(progress)) bar(force = new_stage || st$progress >= 1)
    else if (new_stage) out_line(stage)
    invisible(NULL)
  }
  .monitor_add_methods(self, catch = function() NULL)
}

#' The methods every panel has, built on its update() (shared with the full client).
.monitor_add_methods <- function(self, catch, on_error = NULL, flush = function(timeout = 5) invisible(TRUE)) {
  self$ok       <- function(stage = NULL, ...) self$update("green", stage = stage, clear = "error", ...)
  self$stage    <- function(msg, ...) self$update("green", stage = msg, clear = "error", ...)
  self$warn     <- function(msg, ...) self$update("yellow", stage = msg, ...)
  self$progress <- function(frac, stage = NULL, ...) self$update("green", progress = frac, stage = stage, ...)
  self$stats    <- function(...) self$update(...)
  self$done     <- function(msg = "done", ...) {
    self$update("green", stage = msg, progress = 1, clear = "error", ...)
    self$flush()
  }
  self$error    <- function(msg, ...) {
    self$update("red", error = msg, ...)
    self$flush()
  }
  self$stopped  <- function(msg = "stopped", ...) self$error(msg, ...)
  self$flush    <- flush
  #' Run `expr` as a named step; reports the error (and re-raises it) if it fails. Returns its value.
  self$track <- function(stage, expr, done_msg = NULL) {
    self$stage(stage)
    value <- withCallingHandlers(expr, error = function(e) {
      self$error(paste0(stage, ": ", conditionMessage(e)))
      if (!is.null(on_error)) on_error(conditionMessage(e))
    })
    self$ok(if (is.null(done_msg)) paste(stage, "✓") else done_msg)
    invisible(value)
  }
  self$catch_errors <- function() { catch(); invisible(self) }
  structure(self, class = "monitor_panel")
}

print.monitor_panel <- function(x, ...) {
  where <- if (isTRUE(x$monitor$connected)) x$monitor$url else "the console"
  cat("<monitor panel '", x$id, "' on ", where, ">\n", sep = "")
  invisible(x)
}

if (!exists("monitor_connect", mode = "function")) {   # the full client isn't loaded
  .monitor_full <- path.expand(Sys.getenv("MONITOR_R_CLIENT"))
  if (nzchar(.monitor_full) && file.exists(.monitor_full)) {
    source(.monitor_full, local = environment())
  } else {
    monitor_panel <- function(id, name = NULL, group = NULL, priority = NULL, stale_after = NULL,
                              url = NULL, catch_errors = TRUE, ...) .monitor_console_panel(id, name)
  }
  rm(.monitor_full)
}
