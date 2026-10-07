# Exercised by tests/test_r_client.py against a live server (MONITOR_URL / MONITOR_TOKEN set).
args <- commandArgs(trailingOnly = TRUE)
source(args[1])  # path to monitor_client.R
mode <- args[2]

get_panel <- function(id) .monitor_request(monitor_connect(), "GET", paste0("/api/panels/", id))
check <- function(cond, what) if (!isTRUE(cond)) { message("FAILED: ", what); quit(status = 2) }

if (mode == "basic") {
  run <- monitor_panel("laptop-r-fit", name = "R fit", group = "laptop", priority = TRUE, stale_after = "2h")
  run$stage("loading data")
  run$progress(0.25, stage = "region 1 of 4", n_results = 1L, loss = 0.5)
  run$flush()
  p <- get_panel("laptop-r-fit")
  check(p$status == "green" && p$stage == "region 1 of 4" && p$progress == 0.25, "progress")
  check(p$stats$n_results == 1 && p$stats$loss == 0.5 && p$name == "R fit" && isTRUE(p$priority), "stats/definition")

  v <- run$track("fitting", 21 * 2)
  run$flush()
  check(v == 42 && get_panel("laptop-r-fit")$status == "green", "track ok returns value")

  failed <- tryCatch(run$track("writing outputs", stop("disk full")), error = function(e) conditionMessage(e))
  p <- get_panel("laptop-r-fit")      # error() flushes by itself
  check(failed == "disk full" && p$status == "red" && p$error == "writing outputs: disk full", "track error")

  run$warn("slow convergence")
  run$flush()
  check(get_panel("laptop-r-fit")$status == "yellow", "warn")
  run$done(n_results = 4L)            # done() flushes by itself
  p <- get_panel("laptop-r-fit")
  check(p$status == "green" && p$progress == 1 && is.null(p$error) && p$stats$n_results == 4, "done")

  # a token can't touch panels outside its scope: warning, no error
  w <- NULL
  withCallingHandlers({ other <- monitor_panel("other-thing"); other$ok(); other$flush() },
                      warning = function(x) { w <<- conditionMessage(x); invokeRestart("muffleWarning") })
  check(grepl("may not update", w), "scope warning")

  # server unreachable: no error, script carries on
  down <- monitor_panel("laptop-x", monitor = monitor_connect("http://127.0.0.1:9", quiet = TRUE))
  down$ok("hi")
  down$flush(timeout = 1)             # no error raised: the script carries on
  cat("R CLIENT OK\n")
}

if (mode == "loop") {  # tight loop: cheap calls, few sends, final values arrive
  run <- monitor_panel("laptop-r-loop", monitor = monitor_connect(min_interval = 0.5))
  n <- 0L
  t0 <- proc.time()[["elapsed"]]
  while (proc.time()[["elapsed"]] - t0 < 1.5) {
    n <- n + 1L
    run$progress(min(n / 1e7, 0.99), stage = paste("iteration", n), n = n)
  }
  per_call_us <- (proc.time()[["elapsed"]] - t0) / n * 1e6
  sends <- run$monitor$state$n_sent
  run$done(n = n)
  p <- get_panel("laptop-r-loop")
  cat(sprintf("calls=%d per_call_us=%.1f sends=%d\n", n, per_call_us, sends))
  check(p$stats$n == n && p$progress == 1 && p$stage == "done", "final values")
  check(sends <= 6, "throttled")
  check(per_call_us < 200, "cheap calls")
  cat("R LOOP OK\n")
}

if (mode == "exit") {  # ends without done()/flush(): pending updates still go out at exit
  run <- monitor_panel("laptop-r-exit", monitor = monitor_connect(min_interval = 60))
  run$stage("starting")
  for (i in 1:500) run$progress(i / 1000, n = i)
}

if (mode == "merge") {  # merge each sequence of updates in a JSON file; print the merged bodies
  seqs <- jsonlite::fromJSON(args[3], simplifyVector = FALSE)
  out <- lapply(seqs, function(s) Reduce(function(acc, u) .monitor_merge(acc, u), s, list()))
  cat("[", paste(vapply(out, function(b) if (length(b)) as.character(.monitor_json(b)) else "{}", ""),
                 collapse = ","), "]")
}

if (mode == "crash") {  # run under Rscript: an uncaught error should turn the panel red
  run <- monitor_panel("laptop-r-cron")
  run$catch_errors()
  run$stage("processing")
  x <- log(-1:1)
  stop("input file missing: data.csv")
}

if (mode == "crash-in-track") {  # the step name from track() should survive catch_errors()
  run <- monitor_panel("laptop-r-cron2")
  run$catch_errors()
  run$track("downloading", stop("timeout from server"))
}
