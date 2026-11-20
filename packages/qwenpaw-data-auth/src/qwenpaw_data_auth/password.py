"""
Password hashing (bcrypt, cost >= 12) and strength validation.
"""

from __future__ import annotations

import re
import string

import bcrypt

_BCRYPT_ROUNDS = 12

COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password",
        "123456",
        "12345678",
        "qwerty",
        "abc123",
        "monkey",
        "1234567",
        "letmein",
        "trustno1",
        "dragon",
        "baseball",
        "iloveyou",
        "master",
        "sunshine",
        "ashley",
        "bailey",
        "passw0rd",
        "shadow",
        "123123",
        "654321",
        "superman",
        "qazwsx",
        "michael",
        "football",
        "password1",
        "password123",
        "batman",
        "login",
        "welcome",
        "solo",
        "hello",
        "charlie",
        "donald",
        "loveme",
        "princess",
        "starwars",
        "admin",
        "admin123",
        "root",
        "toor",
        "pass",
        "test",
        "guest",
        "changeme",
        "default",
        "12345",
        "123456789",
        "1234567890",
        "password2",
        "qwerty123",
        "1q2w3e4r",
        "aa123456",
        "000000",
        "111111",
        "121212",
        "888888",
        "666666",
        "987654321",
        "1qaz2wsx",
        "qwe123",
        "1q2w3e",
        "zxcvbnm",
        "asdfghjkl",
        "abcd1234",
        "p@ssw0rd",
        "p@ssword",
        "passwd",
        "pass123",
        "pass1234",
        "access",
        "mustang",
        "letmein1",
        "joshua",
        "michael1",
        "jordan",
        "hunter",
        "buster",
        "soccer",
        "harley",
        "ranger",
        "thomas",
        "george",
        "robert",
        "daniel",
        "andrea",
        "andrew",
        "nicole",
        "jessica",
        "jennifer",
        "amanda",
        "melissa",
        "michelle",
        "william",
        "computer",
        "internet",
        "qwert",
        "asdfgh",
        "zxcvbn",
        "corvette",
        "1234",
        "12341234",
        "iloveu",
        "trustno",
        "freedom",
        "whatever",
        "ginger",
        "pepper",
        "summer",
        "winter",
        "spring",
        "autumn",
        "diamond",
        "matrix",
        "killer",
        "phoenix",
        "thunder",
        "austin",
        "jasper",
        "cookie",
    }
)


def hash_password(password: str) -> str:
    # bcrypt only consumes 72 bytes; reject longer input instead of truncating it.
    if len(password.encode()) > 72:
        raise ValueError("Password must not exceed 72 UTF-8 bytes")
    salt = bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
    return bcrypt.hashpw(password.encode(), salt).decode()


def verify_password(password: str, hashed: str) -> bool:
    # Malformed hashes and over-long input are a failed login, never an error.
    try:
        if not hashed or len(password.encode()) > 72:
            return False
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except (ValueError, TypeError, UnicodeError):
        return False


def validate_password_strength(password: str) -> list[str]:
    """Return a list of violation reasons.  Empty list = password is valid."""
    violations: list[str] = []

    if len(password) < 8:
        violations.append("Password must be at least 8 characters")
    if len(password.encode()) > 72:
        violations.append("Password must not exceed 72 UTF-8 bytes")

    categories = 0
    if re.search(r"[A-Z]", password):
        categories += 1
    if re.search(r"[a-z]", password):
        categories += 1
    if re.search(r"[0-9]", password):
        categories += 1
    if re.search(r"[" + re.escape(string.punctuation) + r"]", password):
        categories += 1
    if categories < 3:
        violations.append(
            "Password must contain at least 3 of: uppercase, lowercase, digit, special character"
        )

    if password.lower() in COMMON_PASSWORDS:
        violations.append("Password is too common")

    return violations
