# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: MIT
"""Azure Functions app: daily collection at 06:00 JST plus an on-demand HTTP trigger."""

from __future__ import annotations

import json
import logging

import azure.functions as func
from azure.identity import DefaultAzureCredential

from collector.pipeline import Collector, CollectorConfig

app = func.FunctionApp()

# Flex Consumption on Linux evaluates CRON in UTC: 21:00 UTC = 06:00 JST (Japan has no DST).
DAILY_SCHEDULE = "0 0 21 * * *"


def _collector() -> Collector:
    return Collector(CollectorConfig.from_env(), DefaultAzureCredential())


@app.timer_trigger(schedule=DAILY_SCHEDULE, arg_name="timer", run_on_startup=False, use_monitor=True)
def daily_collect(timer: func.TimerRequest) -> None:
    if timer.past_due:
        logging.warning("daily_collect is running late (past due).")
    run = _collector().run(trigger="timer")
    logging.info("daily_collect totals: %s", json.dumps(run["totals"]))


@app.route(route="collect", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def collect_now(req: func.HttpRequest) -> func.HttpResponse:
    """Manual (re)run. Body (optional): {"sourceIds": ["rss-..."]}."""
    try:
        body = req.get_json() if req.get_body() else {}
    except ValueError:
        return func.HttpResponse("invalid JSON body", status_code=400)
    source_ids = body.get("sourceIds") if isinstance(body, dict) else None
    run = _collector().run(source_ids=source_ids or None, trigger="http")
    summary = {k: run[k] for k in ("id", "runDate", "totals", "indexer", "sources")}
    return func.HttpResponse(json.dumps(summary, ensure_ascii=False), mimetype="application/json")
