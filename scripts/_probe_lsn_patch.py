#!/usr/bin/env python3
"""One-off P3 probe: why do correct-LOOKING LSN patches fail the lag oracle?

Deploy one LSN patch, inject 20s lag, deposit, then directly compare the
primary's pg_current_wal_lsn() against the replica's pg_last_wal_replay_lsn()
alongside the app's chosen source. Decides model-bug vs harness-artifact.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from harness.archetype_spec import ARCHETYPE_D_STALE
from harness.lifecycle import patch_session

os.environ.setdefault("API_URL", ARCHETYPE_D_STALE.gateway_url)
os.environ.setdefault("PRIMARY_URL", "postgresql://bench:bench@localhost:5435/ledger")
os.environ.setdefault("REPLICA_URL", "postgresql://bench:bench@localhost:5436/ledger")
os.environ.setdefault("TOXIPROXY_URL", ARCHETYPE_D_STALE.toxiproxy_url or "http://localhost:8474")
sys.path.insert(0, str(ARCHETYPE_D_STALE.root))

from tests.helpers import (  # noqa: E402
    deposit,
    read_balance,
    reset_account,
    wait_for_api,
    wait_for_replica_value,
    _pg,
    PRIMARY_URL,
    REPLICA_URL,
)
from tests.replication_chaos import add_replication_lag, reset_replication  # noqa: E402


def _lsn(dsn: str, fn: str) -> str:
    conn = _pg(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {fn}()::text")
            return cur.fetchone()[0]
    finally:
        conn.close()


def _diff(dsn: str, a: str, b: str) -> float:
    conn = _pg(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_wal_lsn_diff(%s, %s)", (a, b))
            return float(cur.fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    target = Path(sys.argv[1])
    patch_text = (target / "model_patch.diff").read_text(encoding="utf-8")
    with patch_session(patch_text, spec=ARCHETYPE_D_STALE):
        wait_for_api(timeout=120)
        time.sleep(2)
        reset_replication()
        reset_account(1000)
        wait_for_replica_value(1000)

        print("== before lag ==")
        print("  primary pg_current_wal_lsn  :", _lsn(PRIMARY_URL, "pg_current_wal_lsn"))
        print("  replica pg_last_wal_replay  :", _lsn(REPLICA_URL, "pg_last_wal_replay_lsn"))

        add_replication_lag(20000)
        ack = deposit(500)
        print("\n== deposit ack ==", ack)

        prim_after = _lsn(PRIMARY_URL, "pg_current_wal_lsn")
        repl_after = _lsn(REPLICA_URL, "pg_last_wal_replay_lsn")
        gap = _diff(PRIMARY_URL, prim_after, repl_after)
        print("\n== right after deposit (lag active) ==")
        print("  primary pg_current_wal_lsn     :", prim_after)
        print("  replica pg_last_wal_replay_lsn :", repl_after)
        print(f"  primary - replica gap (bytes)  : {gap:.0f}  (>0 => replica behind)")

        time.sleep(6)
        bal = read_balance()
        repl_after2 = _lsn(REPLICA_URL, "pg_last_wal_replay_lsn")
        gap2 = _diff(PRIMARY_URL, prim_after, repl_after2)
        print("\n== after 6s wait ==")
        print("  replica pg_last_wal_replay_lsn :", repl_after2)
        print(f"  primary(deposit) - replica gap : {gap2:.0f}  (>0 => replica still behind the deposit)")
        print("  /balance ->", bal)
        print("\nVERDICT:",
              "replica BEHIND but app read replica => MODEL BUG"
              if gap2 > 0 and bal.get("source") == "replica"
              else ("replica CAUGHT UP => harness/lag artifact"
                    if gap2 <= 0 else "app correctly read primary"))
        reset_replication()
    return 0


if __name__ == "__main__":
    sys.exit(main())
