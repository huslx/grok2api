#!/usr/bin/env python3
"""Convert data/oidc_auth.json → Sub2API import/export JSON.

Input (runtime OIDC disk cache written by scripts/sso_to_oidc.py):

  {
    "version": 1,
    "entries": {
      "<sso_sha256>": {
        "access_token": "...",
        "refresh_token": "...",
        "expires_at": 1783765907.0,
        "user_id": "...",
        "team_id": "...",
        "scope": "...",
        ...
      }
    },
    "updated_at": ...
  }

Output (Sub2API data-import format):

  {
    "exported_at": "2026-07-14T09:59:20Z",
    "proxies": [],
    "accounts": [
      {
        "name": "grok-<user_id_prefix>",
        "platform": "grok",
        "type": "oauth",
        "expires_at": 1783765907,
        "auto_pause_on_expired": true,
        "concurrency": 3,
        "priority": 1,
        "credentials": {
          "access_token": "...",
          "refresh_token": "...",
          "expires_at": 1783765907,
          "user_id": "...",
          "team_id": "...",
          "scope": "...",
          "email": ""
        },
        "extra": {
          "user_id": "...",
          "team_id": "...",
          "source": "oidc_auth"
        }
      }
    ]
  }

Examples:

  # Default: data/oidc_auth.json → data/sub2api_accounts.json
  uv run python scripts/oidc_to_sub2api.py

  # Custom paths / concurrency
  uv run python scripts/oidc_to_sub2api.py \\
    --input data/oidc_auth.json \\
    --output data/sub2api_accounts.json \\
    --concurrency 5 --priority 1

  # Compact JSON
  uv run python scripts/oidc_to_sub2api.py --compact
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.dataplane.reverse.protocol.xai_oidc import decode_jwt_claims  # noqa: E402


def _as_unix_int(ts: Any) -> int | None:
    """Normalize expires_at / updated_at to integer unix seconds."""
    if ts is None or ts == "":
        return None
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        # ISO-8601
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return int(datetime.fromisoformat(s).timestamp())
        except ValueError:
            pass
        try:
            return int(float(s))
        except ValueError:
            return None
    try:
        v = int(float(ts))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def _iso_utc_z(ts: float | None = None) -> str:
    t = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_oidc_entries(path: Path) -> list[dict[str, Any]]:
    """Load oidc_auth.json; return list of raw credential dicts."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if not isinstance(raw, dict):
        raise ValueError(f"unsupported oidc auth JSON type: {type(raw).__name__}")

    entries = raw.get("entries")
    if isinstance(entries, dict):
        return [e for e in entries.values() if isinstance(e, dict)]
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)]

    # Already a sub2api export
    if isinstance(raw.get("accounts"), list):
        out: list[dict[str, Any]] = []
        for acc in raw["accounts"]:
            if not isinstance(acc, dict):
                continue
            creds = acc.get("credentials")
            if isinstance(creds, dict):
                out.append(creds)
            elif acc.get("access_token") or acc.get("refresh_token"):
                out.append(acc)
        if out:
            return out

    # grokcli object map: { "issuer::uid": {...}, ... }
    if raw and all(isinstance(v, dict) for v in raw.values()):
        out = []
        for k, v in raw.items():
            if k in {"version", "updated_at", "entries", "exported_at", "proxies", "accounts"}:
                continue
            if isinstance(v, dict) and (
                v.get("access_token") or v.get("key") or v.get("refresh_token")
            ):
                # Normalize key → access_token
                if v.get("key") and not v.get("access_token"):
                    v = {**v, "access_token": v["key"]}
                out.append(v)
        if out:
            return out

    raise ValueError(f"no entries found in {path}")


def to_sub2api_account(
    cred: dict[str, Any],
    *,
    concurrency: int = 3,
    priority: int = 1,
    auto_pause_on_expired: bool = True,
    name_prefix: str = "grok",
    source: str = "oidc_auth",
) -> dict[str, Any] | None:
    """Map one oidc_auth entry → Sub2API account object."""
    access = str(cred.get("access_token") or cred.get("key") or "").strip()
    refresh = str(cred.get("refresh_token") or "").strip()
    if not access and not refresh:
        return None

    claims = decode_jwt_claims(access) if access else {}

    user_id = str(
        cred.get("user_id")
        or claims.get("sub")
        or claims.get("principal_id")
        or ""
    ).strip()
    team_id = str(cred.get("team_id") or claims.get("team_id") or "").strip()
    email = str(cred.get("email") or claims.get("email") or "").strip()
    scope = str(cred.get("scope") or claims.get("scope") or "").strip()

    exp = _as_unix_int(cred.get("expires_at"))
    if exp is None:
        exp = _as_unix_int(claims.get("exp"))

    # Prefer email, then short user_id for display name
    if email:
        name = email
    elif user_id:
        short = user_id.replace("-", "")[:8]
        name = f"{name_prefix}-{short}"
    else:
        tail = (refresh or access)[-8:]
        name = f"{name_prefix}-{tail}"

    credentials: dict[str, Any] = {}
    if access:
        credentials["access_token"] = access
    if refresh:
        credentials["refresh_token"] = refresh
    if exp is not None:
        credentials["expires_at"] = exp
    if user_id:
        credentials["user_id"] = user_id
    if team_id:
        credentials["team_id"] = team_id
    if scope:
        credentials["scope"] = scope
    if email:
        credentials["email"] = email

    extra: dict[str, Any] = {"source": source}
    if user_id:
        extra["user_id"] = user_id
    if team_id:
        extra["team_id"] = team_id
    if email:
        extra["email"] = email
    sso_prefix = str(cred.get("sso_prefix") or "").strip()
    if sso_prefix:
        extra["sso_prefix"] = sso_prefix

    account: dict[str, Any] = {
        "name": name,
        "platform": "grok",
        "type": "oauth",
        "auto_pause_on_expired": bool(auto_pause_on_expired),
        "concurrency": int(concurrency),
        "priority": int(priority),
        "credentials": credentials,
        "extra": extra,
    }
    if exp is not None:
        account["expires_at"] = exp
    return account


def convert(
    creds: list[dict[str, Any]],
    *,
    concurrency: int = 3,
    priority: int = 1,
    auto_pause_on_expired: bool = True,
    name_prefix: str = "grok",
    dedupe: bool = True,
    sort: bool = True,
) -> list[dict[str, Any]]:
    accounts: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0

    for cred in creds:
        item = to_sub2api_account(
            cred,
            concurrency=concurrency,
            priority=priority,
            auto_pause_on_expired=auto_pause_on_expired,
            name_prefix=name_prefix,
        )
        if item is None:
            skipped += 1
            continue

        if dedupe:
            # Prefer user_id; fall back to refresh/access fingerprint
            c = item["credentials"]
            key = str(
                c.get("user_id")
                or c.get("refresh_token")
                or c.get("access_token")
                or item["name"]
            )
            if key in seen:
                skipped += 1
                continue
            seen.add(key)

        accounts.append(item)

    if sort:
        accounts.sort(
            key=lambda a: (
                str((a.get("credentials") or {}).get("user_id") or ""),
                str(a.get("name") or ""),
            )
        )
    return accounts


def build_export(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "exported_at": _iso_utc_z(),
        "proxies": [],
        "accounts": accounts,
    }


def _write_json(path: Path, payload: Any, *, compact: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Convert oidc_auth.json → Sub2API import JSON (platform=grok, type=oauth)"
    )
    ap.add_argument(
        "--input",
        "-i",
        type=Path,
        default=_ROOT / "data" / "oidc_auth.json",
        help="Source OIDC cache (default: data/oidc_auth.json)",
    )
    ap.add_argument(
        "--output",
        "-o",
        type=Path,
        default=_ROOT / "data" / "sub2api_accounts.json",
        help="Output Sub2API export JSON (default: data/sub2api_accounts.json)",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Account concurrency (default: 3)",
    )
    ap.add_argument(
        "--priority",
        type=int,
        default=1,
        help="Account priority, lower = higher priority (default: 1)",
    )
    ap.add_argument(
        "--name-prefix",
        default="grok",
        help="Name prefix when email is missing (default: grok)",
    )
    ap.add_argument(
        "--no-auto-pause",
        action="store_true",
        help="Disable auto_pause_on_expired",
    )
    ap.add_argument(
        "--keep-dupes",
        action="store_true",
        help="Do not dedupe by user_id / token",
    )
    ap.add_argument(
        "--no-sort",
        action="store_true",
        help="Do not sort by user_id",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Only convert first N entries after load (0 = all)",
    )
    ap.add_argument(
        "--compact",
        action="store_true",
        help="Write minified JSON (no indent)",
    )
    args = ap.parse_args()

    if not args.input.is_file():
        print(f"input not found: {args.input}", file=sys.stderr)
        return 2

    try:
        creds = load_oidc_entries(args.input)
    except Exception as exc:  # noqa: BLE001
        print(f"failed to load {args.input}: {exc}", file=sys.stderr)
        return 2

    if args.limit and args.limit > 0:
        creds = creds[: args.limit]

    accounts = convert(
        creds,
        concurrency=args.concurrency,
        priority=args.priority,
        auto_pause_on_expired=not args.no_auto_pause,
        name_prefix=args.name_prefix,
        dedupe=not args.keep_dupes,
        sort=not args.no_sort,
    )
    payload = build_export(accounts)
    _write_json(args.output, payload, compact=bool(args.compact))

    with_refresh = sum(
        1 for a in accounts if (a.get("credentials") or {}).get("refresh_token")
    )
    with_access = sum(
        1 for a in accounts if (a.get("credentials") or {}).get("access_token")
    )

    print(
        f"converted {len(accounts)}/{len(creds)} entries "
        f"(access={with_access}, refresh={with_refresh})\n"
        f"  input:  {args.input}\n"
        f"  output: {args.output}  ({args.output.stat().st_size} bytes)\n"
        f"  format: Sub2API {{exported_at, proxies, accounts}}  platform=grok type=oauth",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
