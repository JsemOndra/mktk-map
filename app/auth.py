"""Password for the mktk-map web UI — scrypt hash (stdlib only).

Generate the hash for .env:
    docker compose run --rm --no-deps mktk-map python auth.py
Format: scrypt:N:r:p:salt:hash (base64url, without the '$' character, which
docker compose would interpolate in .env).
"""

import base64
import getpass
import hashlib
import hmac
import os
import sys

_N, _R, _P = 2 ** 15, 8, 1
_MAXMEM = 64 * 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P,
                        maxmem=_MAXMEM, dklen=32)
    return f"scrypt:{_N}:{_R}:{_P}:{_b64(salt)}:{_b64(dk)}"


def verify(pw: str, stored: str) -> bool:
    try:
        alg, n, r, p, salt, dk = stored.split(":")
        if alg != "scrypt":
            return False
        calc = hashlib.scrypt(pw.encode(), salt=_unb64(salt), n=int(n), r=int(r),
                              p=int(p), maxmem=_MAXMEM, dklen=len(_unb64(dk)))
        return hmac.compare_digest(calc, _unb64(dk))
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":
    a = getpass.getpass("new mktk-map password: ")
    b = getpass.getpass("znovu: ")
    if not a or a != b:
        sys.exit("passwords do not match")
    print("\nPut this into .env:\nAPP_PASS_HASH=" + hash_password(a))
