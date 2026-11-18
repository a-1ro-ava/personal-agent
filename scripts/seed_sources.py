#!/usr/bin/env python3
# Copyright (c) 2026 a-1ro-ava
# SPDX-License-Identifier: MIT
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "azure-cosmos>=4.9",
#     "azure-identity>=1.19",
# ]
# ///
"""Register collection targets from a JSON file into the Cosmos DB ``sources`` container.

Usage:
    uv run scripts/seed_sources.py                     # add config/sources.seed.json entries that do not exist yet
    uv run scripts/seed_sources.py --update            # also overwrite existing entries
    uv run scripts/seed_sources.py --if-empty          # only when no source exists yet
    uv run scripts/seed_sources.py --file my.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity import AzureCliCredential, ChainedTokenCredential, DefaultAzureCredential

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src" / "shared"))

from pa_shared.sources import InvalidSourceError, new_source  # noqa: E402

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ERROR = 2

logger = logging.getLogger(__name__)


def create_parser() -> argparse.ArgumentParser:
    """Create and configure argument parser."""
    parser = argparse.ArgumentParser(description="Seed collection targets into Cosmos DB.")
    parser.add_argument("--file", type=Path, default=REPO_ROOT / "config" / "sources.seed.json")
    parser.add_argument("--if-empty", action="store_true", help="Skip when the sources container already has items.")
    parser.add_argument("--update", action="store_true", help="Overwrite sources that already exist (resets lastRun).")
    return parser


def run(args: argparse.Namespace) -> int:
    """Upsert the sources defined in the seed file."""
    endpoint = os.environ.get("COSMOS_ENDPOINT", "").strip()
    database = os.environ.get("COSMOS_DATABASE", "personal-agent")
    if not endpoint:
        logger.error("COSMOS_ENDPOINT is required.")
        return EXIT_ERROR
    cred = ChainedTokenCredential(AzureCliCredential(), DefaultAzureCredential(exclude_cli_credential=True))
    container = CosmosClient(endpoint, credential=cred).get_database_client(database).get_container_client("sources")

    if args.if_empty:
        count = list(container.query_items("SELECT VALUE COUNT(1) FROM c", enable_cross_partition_query=True))
        if count and count[0] > 0:
            logger.info("sources already has %s item(s); skip seeding.", count[0])
            return EXIT_SUCCESS

    entries = json.loads(args.file.read_text(encoding="utf-8"))
    for entry in entries:
        try:
            doc = new_source(
                entry["kind"],
                entry["target"],
                entry.get("name", ""),
                tags=entry.get("tags"),
                max_items=entry.get("maxItems", 20),
                origin={"type": "seed", "file": args.file.name},
            )
        except (InvalidSourceError, KeyError) as e:
            logger.warning("skip invalid entry %s: %s", entry, e)
            continue
        if not args.update:
            try:
                container.read_item(doc["id"], partition_key=doc["id"])
                logger.info("exists %s (%s %s); use --update to overwrite", doc["id"], doc["kind"], doc["target"])
                continue
            except CosmosResourceNotFoundError:
                pass
        container.upsert_item(doc)
        logger.info("upserted %s (%s %s)", doc["id"], doc["kind"], doc["target"])
    return EXIT_SUCCESS


def main() -> int:
    """Main entry point with error handling."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    try:
        return run(create_parser().parse_args())
    except KeyboardInterrupt:
        return 130
    except Exception as e:  # noqa: BLE001
        logger.exception("Seeding failed: %s", e)
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
