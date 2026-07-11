#!/usr/bin/env python3
"""Batch convert grok.com / accounts.x.ai SSO cookies → OIDC tokens (Device Flow).

Same protocol used by HM2899/grokcli-2api (`sso_to_auth_json.py` / grok-build-auth):

  1. Validate SSO on https://accounts.x.ai/
  2. Request OAuth device code (client_id = grok-cli public client)
  3. Auto-approve device with SSO session
  4. Poll /oauth2/token → access_token + refresh_token

Outputs:
  - data/oidc_auth.json   (used by app.dataplane.reverse.protocol.xai_oidc at runtime)
  - optional grokcli-style auth.json (--auth-json)
  - optional per-user files (--out-dir)

Examples:

  # Convert first 10 active accounts from local SQLite
  uv run python scripts/sso_to_oidc.py --from-db --limit 10 --workers 2

  # From a SSO list file (one JWT per line, or email----sso)
  uv run python scripts/sso_to_oidc.py --sso-file ./sso.txt --workers 4

  # Single cookie
  uv run python scripts/sso_to_oidc.py --sso-cookie 'eyJ...'

  # Also write grokcli-compatible auth.json
  uv run python scripts/sso_to_oidc.py --from-db --limit 5 --auth-json data/auth.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Allow `uv run python scripts/sso_to_oidc.py` from repo root
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.dataplane.reverse.protocol.xai_oidc import (  # noqa: E402
    GROK_CLI_CLIENT_ID,
    OIDC_ISSUER,
    cache_put,
    decode_jwt_claims,
    load_disk_cache,
    save_disk_entry,
    sso_key,
    sso_to_oidc,
)
from app.platform.errors import UpstreamError  # noqa: E402


def _normalize_sso(raw: str) -> str:
    s = raw.strip()
    if s.startswith("sso="):
        s = s[4:]
    return s.strip()


def load_sso_file(path: Path) -> list[tuple[str, str]]:
    """Return list of (label, sso)."""
    out: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        label = ""
        if "----" in line:
            parts = line.split("----")
            label = parts[0].strip()
            line = parts[-1].strip()
        elif ":" in line and not line.startswith("eyJ"):
            # email:password:sso  or  email:sso
            parts = line.rsplit(":", 1)
            label = parts[0].strip()
            line = parts[-1].strip()
        out.append((label, _normalize_sso(line)))
    return out


def load_sso_from_db(
    db_path: Path,
    *,
    limit: int = 0,
    status: str = "active",
) -> list[tuple[str, str]]:
    con = sqlite3.connect(str(db_path))
    try:
        sql = "SELECT token FROM accounts WHERE status = ?"
        params: list[object] = [status]
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()
    return [("", _normalize_sso(r[0])) for r in rows if r and r[0]]


def to_auth_entry(cred: dict, *, email: str = "") -> tuple[str, dict]:
    """Grok CLI / grokcli-2api auth.json entry shape."""
    access = str(cred.get("access_token") or "")
    claims = decode_jwt_claims(access)
    user_id = str(cred.get("user_id") or claims.get("sub") or claims.get("principal_id") or "")
    exp = float(cred.get("expires_at") or 0)
    entry = {
        "key": access,
        "auth_mode": "oidc",
        "create_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "user_id": user_id,
        "email": email or claims.get("email") or "",
        "principal_type": claims.get("principal_type") or "User",
        "principal_id": user_id,
        "refresh_token": str(cred.get("refresh_token") or ""),
        "expires_at": exp,
        "oidc_issuer": OIDC_ISSUER,
        "oidc_client_id": GROK_CLI_CLIENT_ID,
        "team_id": str(cred.get("team_id") or claims.get("team_id") or ""),
        "scope": str(cred.get("scope") or claims.get("scope") or ""),
    }
    key = f"{OIDC_ISSUER}::{user_id or GROK_CLI_CLIENT_ID}"
    return key, entry


def merge_auth_json(path: Path, auth_key: str, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
    data[auth_key] = entry
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def process_one(
    index: int,
    total: int,
    label: str,
    sso: str,
    *,
    disk_path: Path,
    auth_json: Path | None,
    out_dir: Path | None,
    skip_existing: bool,
) -> dict:
    tag = label or f"sso#{index}"
    result: dict = {"index": index, "label": tag, "status": "failed"}
    try:
        sk = sso_key(sso)
        existing = load_disk_cache(disk_path).get("entries", {}).get(sk)
        if skip_existing and existing:
            exp = float(existing.get("expires_at") or 0)
            if exp > time.time() + 300:
                result["status"] = "skipped"
                result["user_id"] = existing.get("user_id")
                result["reason"] = "fresh oidc on disk"
                return result

        print(f"[{index}/{total}] converting {tag} ...", flush=True)
        cred = sso_to_oidc(sso)
        cache_put(sso, cred)
        save_disk_entry(sso, cred, path=disk_path)

        auth_key, entry = to_auth_entry(cred, email=label)
        result["user_id"] = entry.get("user_id")
        result["team_id"] = entry.get("team_id")
        result["expires_at"] = entry.get("expires_at")

        if auth_json is not None:
            merge_auth_json(auth_json, auth_key, entry)
            result["auth_json"] = str(auth_json)

        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            uid = entry.get("user_id") or sk[:12]
            p = out_dir / f"{uid}.json"
            p.write_text(
                json.dumps({auth_key: entry}, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            result["file"] = str(p)

        result["status"] = "ok"
        print(
            f"  ✅ [{index}] user={result.get('user_id','')[:12]} "
            f"team={str(result.get('team_id') or '')[:12]}",
            flush=True,
        )
        return result
    except UpstreamError as exc:
        result["error"] = str(exc)
        print(f"  ❌ [{index}] {exc}", flush=True)
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  ❌ [{index}] {result['error']}", flush=True)
        return result


def main() -> int:
    ap = argparse.ArgumentParser(
        description="SSO → OIDC token converter (Device Flow / grok-cli client)"
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--from-db",
        action="store_true",
        help="Read SSO from data/accounts.db (status=active)",
    )
    src.add_argument("--sso-file", type=Path, help="SSO list file")
    src.add_argument("--sso-cookie", help="Single SSO JWT")

    ap.add_argument(
        "--db",
        type=Path,
        default=_ROOT / "data" / "accounts.db",
        help="SQLite path when --from-db (default: data/accounts.db)",
    )
    ap.add_argument("--limit", type=int, default=0, help="Max accounts from DB (0=all)")
    ap.add_argument(
        "--disk",
        type=Path,
        default=_ROOT / "data" / "oidc_auth.json",
        help="Runtime OIDC cache path (default: data/oidc_auth.json)",
    )
    ap.add_argument(
        "--auth-json",
        type=Path,
        default=None,
        help="Also write/merge grokcli-style auth.json",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Also write one {user_id}.json per account",
    )
    ap.add_argument("--workers", type=int, default=2, help="Concurrency (default 2, max 8)")
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip SSO that already has a fresh OIDC entry on disk",
    )
    args = ap.parse_args()

    if args.sso_cookie:
        items = [("", _normalize_sso(args.sso_cookie))]
    elif args.sso_file:
        items = load_sso_file(args.sso_file)
    else:
        if not args.db.is_file():
            print(f"DB not found: {args.db}", file=sys.stderr)
            return 2
        items = load_sso_from_db(args.db, limit=args.limit)

    if not items:
        print("No SSO tokens to convert", file=sys.stderr)
        return 2

    workers = max(1, min(int(args.workers or 2), 8))
    total = len(items)
    print(
        f"SSO → OIDC: {total} token(s), workers={workers}, disk={args.disk}",
        flush=True,
    )

    ok = skip = fail = 0
    results: list[dict] = []

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sso2oidc") as pool:
        futs = {
            pool.submit(
                process_one,
                i,
                total,
                label,
                sso,
                disk_path=args.disk,
                auth_json=args.auth_json,
                out_dir=args.out_dir,
                skip_existing=bool(args.skip_existing),
            ): i
            for i, (label, sso) in enumerate(items, 1)
        }
        for fut in as_completed(futs):
            res = fut.result()
            results.append(res)
            st = res.get("status")
            if st == "ok":
                ok += 1
            elif st == "skipped":
                skip += 1
            else:
                fail += 1

    print(
        f"\nDone: ok={ok} skipped={skip} fail={fail} total={total}\n"
        f"OIDC cache: {args.disk}",
        flush=True,
    )
    if args.auth_json:
        print(f"auth.json:  {args.auth_json}", flush=True)

    # Write a small summary next to the disk cache
    summary = {
        "finished_at": time.time(),
        "ok": ok,
        "skipped": skip,
        "fail": fail,
        "total": total,
        "results": [
            {
                "index": r.get("index"),
                "label": r.get("label"),
                "status": r.get("status"),
                "user_id": r.get("user_id"),
                "error": r.get("error"),
            }
            for r in sorted(results, key=lambda x: int(x.get("index") or 0))
        ],
    }
    summary_path = args.disk.with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"summary:    {summary_path}", flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
