#!/usr/bin/env python3
"""Inspect / refresh local OIDC credentials (data/oidc_auth.json).

OIDC access_tokens expire; runtime only refreshes on demand. This script lists
which accounts are still fresh and can bulk-refresh via refresh_token grant
(no full Device Flow).

Examples:

  # Summary + list (default: show all buckets briefly)
  uv run python scripts/check_oidc.py

  # Only still-valid access tokens
  uv run python scripts/check_oidc.py --only fresh

  # Expired / near-expiry but still refreshable
  uv run python scripts/check_oidc.py --only refreshable

  # Refresh first 20 expired tokens (needs matching SSO in accounts.db to
  # re-key disk; hash-only entries are updated in place)
  uv run python scripts/check_oidc.py --refresh --only expired --limit 20

  # Refresh everything that has a refresh_token and is not fresh
  uv run python scripts/check_oidc.py --refresh --only refreshable --workers 4

  # JSON for automation
  uv run python scripts/check_oidc.py --only fresh --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.dataplane.reverse.protocol.xai_oidc import (  # noqa: E402
    _REFRESH_SKEW_S,
    load_disk_cache,
    refresh_oidc,
    save_disk_entry,
    sso_key,
)
from app.platform.errors import UpstreamError  # noqa: E402


def _normalize_sso(raw: str) -> str:
    s = raw.strip()
    if s.startswith("sso="):
        s = s[4:]
    return s.strip()


def _fmt_duration(seconds: float) -> str:
    sec = int(seconds)
    sign = "-" if sec < 0 else ""
    sec = abs(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{sign}{d}d{h:02d}h"
    if h:
        return f"{sign}{h}h{m:02d}m"
    if m:
        return f"{sign}{m}m{s:02d}s"
    return f"{sign}{s}s"


def _fmt_ts(ts: float) -> str:
    if not ts or ts <= 0:
        return "-"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _bucket(cred: dict[str, Any], *, now: float, skew: float) -> str:
    access = cred.get("access_token") or ""
    if not access:
        return "no_access"
    exp = float(cred.get("expires_at") or 0)
    if exp <= 0:
        return "no_exp"
    if exp > now + skew:
        return "fresh"
    if exp > now:
        return "skew"  # within refresh skew window
    return "expired"


def _load_accounts(db_path: Path) -> dict[str, dict[str, Any]]:
    """sso_sha256 → {sso, status, pool, tags, ...}."""
    if not db_path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    con = sqlite3.connect(str(db_path))
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(accounts)")}
        select = ["token", "status", "pool"]
        optional = [c for c in ("tags", "last_use_at", "state_reason") if c in cols]
        select.extend(optional)
        sql = f"SELECT {', '.join(select)} FROM accounts"
        for row in con.execute(sql):
            token = row[0]
            if not token:
                continue
            sso = _normalize_sso(str(token))
            sk = sso_key(sso)
            info: dict[str, Any] = {
                "sso": sso,
                "status": row[1] or "",
                "pool": row[2] or "",
            }
            for i, name in enumerate(optional, start=3):
                info[name] = row[i]
            out[sk] = info
    finally:
        con.close()
    return out


def _iter_rows(
    disk_path: Path,
    accounts: dict[str, dict[str, Any]],
    *,
    now: float,
    skew: float,
) -> list[dict[str, Any]]:
    data = load_disk_cache(disk_path)
    entries = data.get("entries") or {}
    if not isinstance(entries, dict):
        return []

    rows: list[dict[str, Any]] = []
    for sk, cred in entries.items():
        if not isinstance(sk, str) or not isinstance(cred, dict):
            continue
        exp = float(cred.get("expires_at") or 0)
        acct = accounts.get(sk) or {}
        bucket = _bucket(cred, now=now, skew=skew)
        has_rt = bool(cred.get("refresh_token"))
        rows.append(
            {
                "sso_sha256": sk,
                "sso_prefix": str(cred.get("sso_prefix") or (acct.get("sso") or "")[:16]),
                "user_id": str(cred.get("user_id") or ""),
                "team_id": str(cred.get("team_id") or ""),
                "expires_at": exp,
                "expires_in_s": exp - now if exp else None,
                "bucket": bucket,
                "has_refresh": has_rt,
                "refreshable": has_rt and bucket != "fresh",
                "account_status": acct.get("status") or ("missing" if accounts else "n/a"),
                "pool": acct.get("pool") or "",
                "in_db": sk in accounts,
                "sso": acct.get("sso") or "",
                "updated_at": float(cred.get("updated_at") or 0),
                "_cred": cred,
            }
        )
    rows.sort(key=lambda r: (r["bucket"] != "fresh", -(r["expires_in_s"] or -1e18)))
    return rows


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    only: str,
    account_status: str,
    limit: int,
    prefix: str,
    user_id: str,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        b = r["bucket"]
        if only == "fresh" and b != "fresh":
            continue
        if only == "skew" and b != "skew":
            continue
        if only == "expired" and b != "expired":
            continue
        if only == "refreshable" and not r["refreshable"]:
            continue
        if only == "stale" and b not in ("skew", "expired"):
            continue
        if account_status and account_status != "all":
            if r["account_status"] != account_status:
                continue
        if prefix and not str(r.get("sso_prefix") or "").startswith(prefix):
            continue
        if user_id and not str(r.get("user_id") or "").startswith(user_id):
            continue
        out.append(r)
        if limit and limit > 0 and len(out) >= limit:
            break
    return out


def _summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {
        "total": len(rows),
        "fresh": 0,
        "skew": 0,
        "expired": 0,
        "no_access": 0,
        "no_exp": 0,
        "refreshable": 0,
        "in_db": 0,
        "active": 0,
        "active_fresh": 0,
        "active_refreshable": 0,
    }
    for r in rows:
        b = r["bucket"]
        counts[b] = counts.get(b, 0) + 1
        if r["refreshable"]:
            counts["refreshable"] += 1
        if r["in_db"]:
            counts["in_db"] += 1
        if r["account_status"] == "active":
            counts["active"] += 1
            if b == "fresh":
                counts["active_fresh"] += 1
            if r["refreshable"]:
                counts["active_refreshable"] += 1
    return counts


def _print_table(rows: list[dict[str, Any]], *, show_sso: bool = False) -> None:
    if not rows:
        print("(no matching entries)")
        return
    header = (
        f"{'#':>4}  {'bucket':<10}  {'expires_in':>10}  {'acct':<10}  "
        f"{'pool':<8}  {'rt':^2}  {'user_id':<16}  sso_prefix"
    )
    print(header)
    print("-" * len(header))
    for i, r in enumerate(rows, 1):
        exp_s = r["expires_in_s"]
        exp_txt = _fmt_duration(exp_s) if exp_s is not None else "-"
        print(
            f"{i:>4}  {r['bucket']:<10}  {exp_txt:>10}  {r['account_status']:<10}  "
            f"{(r['pool'] or '-'):<8}  {'Y' if r['has_refresh'] else 'N':^2}  "
            f"{(r['user_id'] or '-')[:16]:<16}  {r['sso_prefix'] or '-'}"
        )
        if show_sso and r.get("sso"):
            print(f"      sso={r['sso'][:48]}...")


def _save_entry_by_hash(
    sk: str,
    cred: dict[str, Any],
    *,
    path: Path,
    sso_prefix: str = "",
) -> None:
    """Upsert oidc_auth entry keyed by known sso_sha256 (no full SSO needed)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Reuse save_disk_entry locking via direct file write with same shape.
    # load/save is process-local; scripts are single-owner.
    from app.dataplane.reverse.protocol.xai_oidc import _DISK_LOCK  # noqa: PLC0415

    with _DISK_LOCK:
        data = load_disk_cache(path)
        entries = data.setdefault("entries", {})
        if not isinstance(entries, dict):
            entries = {}
            data["entries"] = entries
        entries[sk] = {
            "access_token": cred.get("access_token"),
            "refresh_token": cred.get("refresh_token"),
            "expires_at": cred.get("expires_at"),
            "user_id": cred.get("user_id"),
            "team_id": cred.get("team_id"),
            "scope": cred.get("scope"),
            "sso_prefix": sso_prefix or (entries.get(sk) or {}).get("sso_prefix") or "",
            "updated_at": time.time(),
        }
        data["version"] = 1
        data["updated_at"] = time.time()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)


def _refresh_one(
    row: dict[str, Any],
    *,
    disk_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sso_sha256": row["sso_sha256"],
        "user_id": row.get("user_id"),
        "sso_prefix": row.get("sso_prefix"),
        "bucket_before": row["bucket"],
        "status": "failed",
    }
    cred = row.get("_cred") or {}
    if not cred.get("refresh_token"):
        result["error"] = "no refresh_token"
        return result
    if dry_run:
        result["status"] = "dry_run"
        return result
    try:
        refreshed = refresh_oidc(cred)
        sso = row.get("sso") or ""
        if sso:
            save_disk_entry(sso, refreshed, path=disk_path)
        else:
            _save_entry_by_hash(
                row["sso_sha256"],
                refreshed,
                path=disk_path,
                sso_prefix=str(row.get("sso_prefix") or ""),
            )
        exp = float(refreshed.get("expires_at") or 0)
        result["status"] = "ok"
        result["expires_at"] = exp
        result["expires_in_s"] = exp - time.time() if exp else None
        result["user_id"] = refreshed.get("user_id") or result.get("user_id")
        return result
    except UpstreamError as exc:
        result["error"] = str(exc)
        body = ""
        if isinstance(exc.details, dict):
            body = str(exc.details.get("body") or "")
        result["body"] = body[:200]
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check local OIDC validity and optionally refresh via refresh_token"
    )
    ap.add_argument(
        "--disk",
        type=Path,
        default=_ROOT / "data" / "oidc_auth.json",
        help="OIDC cache path (default: data/oidc_auth.json)",
    )
    ap.add_argument(
        "--db",
        type=Path,
        default=_ROOT / "data" / "accounts.db",
        help="Accounts SQLite (default: data/accounts.db)",
    )
    ap.add_argument(
        "--only",
        choices=("all", "fresh", "skew", "expired", "stale", "refreshable"),
        default="all",
        help="Filter listed/refreshed rows (default: all). "
        "stale=skew+expired; refreshable=has RT and not fresh",
    )
    ap.add_argument(
        "--account-status",
        default="active",
        help="Filter by accounts.db status (default: active; use 'all' to include every status / missing)",
    )
    ap.add_argument("--limit", type=int, default=0, help="Max rows to list/refresh (0=all)")
    ap.add_argument("--prefix", default="", help="Filter by sso_prefix startswith")
    ap.add_argument("--user-id", default="", dest="user_id", help="Filter by user_id startswith")
    ap.add_argument(
        "--skew",
        type=float,
        default=float(_REFRESH_SKEW_S),
        help=f"Fresh skew seconds (default: {_REFRESH_SKEW_S:.0f}, same as runtime)",
    )
    ap.add_argument("--refresh", action="store_true", help="Call refresh_token grant for filtered rows")
    ap.add_argument("--dry-run", action="store_true", help="With --refresh: show what would refresh")
    ap.add_argument("--workers", type=int, default=2, help="Refresh concurrency (default 2, max 8)")
    ap.add_argument("--json", action="store_true", help="Machine-readable JSON output")
    ap.add_argument("--show-sso", action="store_true", help="Print SSO snippet for matched DB rows")
    ap.add_argument(
        "--list-limit",
        type=int,
        default=50,
        help="When --only all without --json, cap printed rows (default 50; 0=unlimited)",
    )
    args = ap.parse_args()

    if not args.disk.is_file():
        print(f"OIDC cache not found: {args.disk}", file=sys.stderr)
        return 2

    now = time.time()
    skew = float(args.skew)
    accounts = _load_accounts(args.db)
    all_rows = _iter_rows(args.disk, accounts, now=now, skew=skew)
    counts = _summary(all_rows)

    filtered = _filter_rows(
        all_rows,
        only=args.only,
        account_status=args.account_status,
        limit=args.limit,
        prefix=args.prefix.strip(),
        user_id=args.user_id.strip(),
    )

    # For listing "all", optionally cap display (refresh still uses --limit).
    display_rows = filtered
    if (
        not args.refresh
        and not args.json
        and args.only == "all"
        and not args.limit
        and args.list_limit
        and len(filtered) > args.list_limit
    ):
        # Prefer showing fresh first (already sorted), then a sample of rest.
        fresh = [r for r in filtered if r["bucket"] == "fresh"]
        rest = [r for r in filtered if r["bucket"] != "fresh"]
        remain = max(0, args.list_limit - len(fresh))
        display_rows = fresh + rest[:remain]

    if args.refresh:
        targets = [r for r in filtered if r["refreshable"] or (args.dry_run and r.get("has_refresh"))]
        # Only refresh non-fresh by default; allow explicit fresh with --only fresh? skip.
        targets = [r for r in targets if r["bucket"] != "fresh"]
        if args.only == "fresh":
            print(
                "Nothing to refresh with --only fresh (already valid). "
                "Use --only expired|refreshable|stale|all.",
                file=sys.stderr,
            )
            return 1
        workers = max(1, min(int(args.workers or 2), 8))
        if not args.json:
            print(
                f"OIDC refresh: candidates={len(targets)} workers={workers} "
                f"disk={args.disk} dry_run={bool(args.dry_run)}",
                flush=True,
            )
        results: list[dict[str, Any]] = []
        ok = fail = dry = 0
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="oidc-ref") as pool:
            futs = {
                pool.submit(
                    _refresh_one,
                    row,
                    disk_path=args.disk,
                    dry_run=bool(args.dry_run),
                ): row
                for row in targets
            }
            for fut in as_completed(futs):
                res = fut.result()
                results.append(res)
                st = res.get("status")
                tag = (res.get("user_id") or res.get("sso_prefix") or "?")[:16]
                if st == "ok":
                    ok += 1
                    if not args.json:
                        print(
                            f"  ✅ {tag} expires_in={_fmt_duration(res.get('expires_in_s') or 0)}",
                            flush=True,
                        )
                elif st == "dry_run":
                    dry += 1
                    if not args.json:
                        print(f"  · dry-run {tag}", flush=True)
                else:
                    fail += 1
                    if not args.json:
                        print(f"  ❌ {tag} {res.get('error')}", flush=True)

        payload = {
            "mode": "refresh",
            "disk": str(args.disk),
            "summary_before": counts,
            "candidates": len(targets),
            "ok": ok,
            "fail": fail,
            "dry_run": dry,
            "results": [
                {k: v for k, v in r.items() if not k.startswith("_")}
                for r in results
            ],
        }
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(
                f"\nDone: ok={ok} fail={fail} dry_run={dry} candidates={len(targets)}\n"
                f"OIDC cache: {args.disk}",
                flush=True,
            )
        return 0 if fail == 0 else 1

    # list mode
    public_rows = [{k: v for k, v in r.items() if k != "_cred" and k != "sso"} for r in display_rows]
    if args.show_sso:
        public_rows = [{k: v for k, v in r.items() if k != "_cred"} for r in display_rows]

    payload = {
        "mode": "list",
        "disk": str(args.disk),
        "db": str(args.db),
        "now": now,
        "skew_s": skew,
        "filter": {
            "only": args.only,
            "account_status": args.account_status,
            "limit": args.limit,
            "prefix": args.prefix,
            "user_id": args.user_id,
        },
        "summary": counts,
        "shown": len(display_rows),
        "matched": len(filtered),
        "entries": public_rows,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return 0

    print(f"OIDC cache: {args.disk}")
    print(f"Accounts DB: {args.db} ({'ok' if args.db.is_file() else 'missing'})")
    print(f"Now: {_fmt_ts(now)}  skew={skew:.0f}s")
    print()
    print("Summary (all disk entries):")
    print(
        f"  total={counts['total']}  fresh={counts['fresh']}  skew={counts['skew']}  "
        f"expired={counts['expired']}  refreshable={counts['refreshable']}"
    )
    print(
        f"  in_db={counts['in_db']}  active={counts['active']}  "
        f"active_fresh={counts['active_fresh']}  active_refreshable={counts['active_refreshable']}"
    )
    print()
    print(
        f"Listing: only={args.only} account_status={args.account_status} "
        f"matched={len(filtered)} shown={len(display_rows)}"
        + (
            f" (capped by --list-limit {args.list_limit}; "
            f"use --only fresh|expired or --list-limit 0)"
            if len(display_rows) < len(filtered)
            else ""
        )
    )
    print()
    _print_table(display_rows, show_sso=bool(args.show_sso))
    if counts["fresh"]:
        print()
        print(
            f"Tip: refresh expired tokens with:\n"
            f"  uv run python scripts/check_oidc.py --refresh --only refreshable --limit 20"
        )
    elif counts["refreshable"]:
        print()
        print(
            f"No fresh tokens. {counts['active_refreshable']} active accounts are refreshable:\n"
            f"  uv run python scripts/check_oidc.py --refresh --only refreshable --workers 4"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
