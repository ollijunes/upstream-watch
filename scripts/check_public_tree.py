"""Check reachable Git snapshots and publishable working files for private data."""

import hashlib
import ipaddress
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

PATTERNS = {
    "private network address": re.compile(
        rb"\b(?:10(?:\.[0-9]{1,3}){3}|192\.168(?:\.[0-9]{1,3}){2}|"
        rb"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2}|"
        rb"100\.(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])(?:\.[0-9]{1,3}){2})\b"
    ),
    "personal home directory": re.compile(
        rb"(?<![A-Za-z0-9_./-])(?:/"
        rb"Users/[^/\s\"'<>]+/|/"
        rb"home/[^/\s\"'<>]+/|[Cc]:\\Users\\[^\\\s\"'<>]+\\)"
    ),
}
PRIVATE_FILE = re.compile(
    r"(?i)(?:^|/)(?:drafts(?:/|$)|(?:\.env(?:\..*)?|\.key|config\.toml|"
    r"identity\.toml|services\.csv|tracker\.csv|state\.sqlite3(?:-.*)?|"
    r"\.credentials\.json|auth\.json|\.claude\.json|"
    r"[^/]+\.(?:key|pem|p12|jpg|jpeg|arw|nef|cr3|heic|mp4|sqlite3|db|log|jsonl|af))$)"
)
EMAIL = re.compile(
    rb"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@([A-Z0-9-]+(?:\.[A-Z0-9-]+)+)",
    re.IGNORECASE,
)
PERSONAL_CODE = re.compile(
    rb"(?<![A-Z0-9])([0-9]{2})([0-9]{2})([0-9]{2})([-+A-FYXWVU])"
    rb"([0-9]{3})([0-9A-FHJ-NPR-Y])(?![A-Z0-9])",
    re.IGNORECASE,
)
PHONE = re.compile(rb"(?<![\w+])(?:\+358|0[45]0)(?:[ ()-]*[0-9]){6,}(?![0-9])")
CHECK_CHARACTERS = b"0123456789ABCDEFHJKLMNPRSTUVWXY"
ALLOWED_EMAIL_DOMAINS = {
    b"example.com",
    b"example.org",
    b"example.net",
    b"users.noreply.github.com",
}
ALLOWED_EMAIL_ADDRESSES = {b"noreply@github.com", b"support@github.com"}


def git(*args):
    return subprocess.check_output(["git", *args])  # noqa: S603,S607 - fixed git commands only.


def detect(data):
    findings = [name for name, pattern in PATTERNS.items() if pattern.search(data)]
    if any(
        match[0].lower() not in ALLOWED_EMAIL_ADDRESSES
        and match[1].lower() not in ALLOWED_EMAIL_DOMAINS
        for match in EMAIL.finditer(data)
    ):
        findings.append("non-example email address")
    if any(valid_personal_code(match) for match in PERSONAL_CODE.finditer(data)):
        findings.append("Finnish personal identity code")
    if PHONE.search(data):
        findings.append("Finnish phone number")
    return findings


def valid_personal_code(match):
    day, month, year, separator, individual, check = match.groups()
    century = 1800 if separator == b"+" else 2000 if separator.upper() in b"ABCDEF" else 1900
    try:
        date(century + int(year), int(month), int(day))
    except ValueError:
        return False
    number = int(day + month + year + individual)
    return CHECK_CHARACTERS[number % 31 : number % 31 + 1] == check.upper()


def private_path(path):
    if path in {
        "examples/config.toml",
        "examples/home/.claude/CLAUDE.md",
        "examples/home/.claude/.credentials.json",
        "examples/home/.claude/projects/-home-alex-src-demo/memory/MEMORY.md",
        "examples/home/.claude/projects/-home-alex-src-demo/transcript.jsonl",
        "examples/home/.claude/projects/-tmp-demo/memory/MEMORY.md",
        "examples/home/.codex/AGENTS.md",
        "examples/home/.codex/config.toml",
        "examples/home/.codex/auth.json",
    }:
        return False
    if re.fullmatch(r"examples/[^/]+\.example\.[^/]+", path):
        return False
    return bool(PRIVATE_FILE.search(path)) or bool({".claude", ".codex"} & set(path.split("/")))


def self_test():
    cases = []
    for address in (0x0A000001, 0xC0A80001, 0xAC100001, 0xAC1F0001, 0x64400001, 0x647F0001):
        cases.append((str(ipaddress.IPv4Address(address)), "private network address"))
    for parts, sep in (
        (["", "Users", "fixtureperson", "photo"], "/"),
        (["", "home", "fixtureperson", "photo"], "/"),
        (["C:", "Users", "fixtureperson", "photo"], "\\"),
    ):
        cases.append((sep.join(parts), "personal home directory"))
    for domain in ("unapproved" + ".com", "example.com" + ".unapproved"):
        cases.append(("@".join(("synthetic", domain)), "non-example email address"))
    cases.append(("@".join(("someone", "github.com")), "non-example email address"))
    number = "".join(("01", "01", "00", "123"))
    check = chr(CHECK_CHARACTERS[int(number) % 31])
    for separator in "-+ABCDEFYXWVU":
        cases.append(
            (number[:6] + separator + number[6:] + check, "Finnish personal identity code")
        )
    for prefix in ("+" + "358", "0" + "40", "0" + "50"):
        cases.append((prefix + "123" + "4567", "Finnish phone number"))
        cases.append((prefix + " 123 " + "4567", "Finnish phone number"))
    for content, expected in cases:
        if expected not in detect(content.encode()):
            raise RuntimeError("Privacy detector missed a synthetic canary.")
    private_names = (
        "private.key",
        "auth.json",
        ".credentials.json",
        ".claude.json",
        "transcript.jsonl",
        ".claude/CLAUDE.md",
        ".codex/AGENTS.md",
        ".env",
        "config.toml",
        "photo.jpg",
        "state.sqlite3",
        "private.log",
        "identity.toml",
        "services.csv",
        "tracker.csv",
        "drafts/request.txt",
        "data/drafts/note.txt",
    )
    for name in private_names:
        if not private_path(name):
            raise RuntimeError("Privacy detector missed a private filename canary.")
    permitted = [b"https://service.example.org"]
    permitted.append(b"GitHub <noreply@github.com>")
    permitted.extend(b"synthetic@" + domain for domain in ALLOWED_EMAIL_DOMAINS)
    wrong_check = "0" if check != "0" else "1"
    permitted.append((number[:6] + "A" + number[6:] + wrong_check).encode())
    for value in permitted:
        if detect(value):
            raise RuntimeError("Privacy detector rejected a permitted example.")
    for name in (
        "examples/config.toml",
        "examples/identity.example.toml",
        "examples/services.example.csv",
    ):
        if private_path(name):
            raise RuntimeError("Privacy detector rejected an example filename.")
    print(
        f"Privacy detector: {len(cases) + len(private_names) + len(permitted) + 3} canaries passed."
    )


def main():
    self_test()
    if sys.argv[1:] == ["--self-test"]:
        return 0
    commits = git("rev-list", "--all").decode().splitlines()
    seen = set()
    failures = 0
    for commit in commits:
        metadata = git("show", "-s", "--format=%an <%ae>%n%cn <%ce>%n%B", commit)
        for category in detect(metadata):
            print(f"FAIL: commit {commit[:12]}: {category}; value withheld")
            failures += 1
        for entry in git("ls-tree", "-r", "-z", commit).split(b"\0"):
            if not entry:
                continue
            descriptor, path = entry.split(b"\t", 1)
            _, kind, oid = descriptor.split()
            if kind != b"blob" or (oid, path) in seen:
                continue
            seen.add((oid, path))
            categories = detect(path) + detect(git("cat-file", "blob", oid.decode()))
            if private_path(path.decode(errors="replace")):
                categories.append("private runtime/photo file")
            for category in categories:
                # A path can itself contain a secret; identify only its Git object.
                print(f"FAIL: object {oid.decode()[:12]}: {category}; value withheld")
                failures += 1
    print(
        f"Privacy history: {len(commits)} commits, {len(seen)} file versions, {failures} findings."
    )
    paths = set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0"))
    working_failures = 0
    for raw_path in sorted(paths - {b""}):
        path = Path(raw_path.decode("utf-8"))
        if not path.exists() and not path.is_symlink():
            continue
        categories = detect(raw_path)
        if path.is_symlink():
            categories.append("symbolic link in public tree")
        elif path.is_file():
            categories.extend(detect(path.read_bytes()))
        if private_path(path.as_posix()):
            categories.append("private runtime/photo file")
        for category in categories:
            identifier = hashlib.sha256(raw_path).hexdigest()[:12]
            print(f"FAIL: working file {identifier}: {category}; path and value withheld")
            working_failures += 1
    print(f"Privacy working tree: {len(paths - {b''})} files, {working_failures} findings.")
    failures += working_failures
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
