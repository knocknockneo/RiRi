#!/usr/bin/env python3
"""Mint access tokens. Prints raw tokens ONCE (hand these out on cards);
stores only hashes. Usage: python scripts/make_tokens.py 30 --label fall26-sec01
"""
import argparse, secrets, sys
sys.path.insert(0, ".")
from riri import db

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L ambiguity

def mint() -> str:
    def block():
        return "".join(secrets.choice(ALPHABET) for _ in range(4))
    return f"RIRI-{block()}-{block()}"

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("count", type=int)
    ap.add_argument("--label", default="")
    args = ap.parse_args()
    db.init()
    with db.conn() as c:
        for _ in range(args.count):
            raw = mint()
            c.execute("INSERT INTO tokens VALUES (?,?,?,0)",
                      (db.hash_token(raw), args.label, db.now()))
            print(raw)
    print(f"\n{args.count} tokens minted (label={args.label!r}). "
          "Raw tokens are shown above only once — the DB stores hashes.", file=sys.stderr)
