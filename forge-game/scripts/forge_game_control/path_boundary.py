from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit, urlunsplit


class BoundaryViolation(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _is_within(value: str, boundary: str) -> bool:
    return boundary == "." or value == boundary or value.startswith(boundary + "/")


def normalize_project_path(
    value: str,
    *,
    project_root: str,
    allowed_roots: list[str],
    protected_paths: list[str],
) -> tuple[str, Path]:
    if "\x00" in value or "\\" in value:
        raise BoundaryViolation("path.invalid", "Path contains forbidden characters")
    lexical = PurePosixPath(value)
    if lexical.is_absolute() or ".." in lexical.parts:
        raise BoundaryViolation(
            "path.outside_project", "Only project-relative paths without '..' are allowed"
        )
    normalized = lexical.as_posix()
    if normalized in ("", "."):
        normalized = "."
    elif any(part in ("", ".") for part in lexical.parts):
        raise BoundaryViolation("path.non_canonical", "Path must be canonical")

    normalized_allowed = [_normalize_policy_path(item) for item in allowed_roots]
    if not any(_is_within(normalized, boundary) for boundary in normalized_allowed):
        raise BoundaryViolation(
            "path.not_allowed_by_project_policy",
            f"Path {normalized!r} is outside project policy write roots",
        )
    normalized_protected = [_normalize_policy_path(item) for item in protected_paths]
    if any(_is_within(normalized, boundary) for boundary in normalized_protected):
        raise BoundaryViolation(
            "path.protected_by_project_policy", f"Path {normalized!r} is protected"
        )

    root = Path(project_root)
    if not root.is_absolute() or not root.exists() or not root.is_dir():
        raise BoundaryViolation(
            "project_root.unavailable", "Project root must be an existing absolute directory"
        )
    if root.is_symlink():
        raise BoundaryViolation(
            "project_root.symlink", "Project root must not be a symlink"
        )
    resolved_root = root.resolve(strict=True)
    candidate = resolved_root
    for part in lexical.parts:
        if part == ".":
            continue
        candidate = candidate / part
        if candidate.is_symlink():
            raise BoundaryViolation(
                "path.symlink", f"Project path traverses symlink component {part!r}"
            )
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise BoundaryViolation(
            "path.outside_project", "Resolved path escapes the project root"
        ) from exc
    return normalized, resolved_candidate


def _normalize_policy_path(value: str) -> str:
    if "\x00" in value or "\\" in value:
        raise BoundaryViolation("policy_path.invalid", "Policy path is invalid")
    lexical = PurePosixPath(value)
    if lexical.is_absolute() or ".." in lexical.parts:
        raise BoundaryViolation(
            "policy_path.outside_project", "Policy paths must be project-relative"
        )
    normalized = lexical.as_posix()
    return "." if normalized in ("", ".") else normalized


def path_is_within_roots(path: Path, roots: list[str]) -> bool:
    resolved_path = path.resolve(strict=False)
    for root_value in roots:
        root = Path(root_value)
        if not root.is_absolute():
            continue
        resolved_root = root.resolve(strict=False)
        try:
            resolved_path.relative_to(resolved_root)
            return True
        except ValueError:
            continue
    return False


def normalize_https_url(value: str) -> tuple[str, str]:
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise BoundaryViolation("network.url_invalid", "Only absolute HTTPS URLs are allowed")
    if parsed.username or parsed.password:
        raise BoundaryViolation(
            "network.url_credentials", "Credentials are forbidden in action target URLs"
        )
    if parsed.fragment:
        raise BoundaryViolation("network.url_fragment", "URL fragments are not allowed")
    host = parsed.hostname.lower().rstrip(".")
    normalized = urlunsplit(
        ("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, "")
    )
    return normalized, host


def domain_is_allowed(host: str, allowed_domains: list[str]) -> bool:
    for value in allowed_domains:
        allowed = value.lower().rstrip(".")
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


_GIT_REF = re.compile(r"^(?:refs/(?:heads|tags)/)?[A-Za-z0-9][A-Za-z0-9._/-]*$")


def normalize_git_ref(value: str) -> str:
    forbidden = ("..", "@{", "//", "\\", "~", "^", ":", "?", "*", "[")
    if (
        not _GIT_REF.fullmatch(value)
        or any(item in value for item in forbidden)
        or value.endswith(("/", ".", ".lock"))
    ):
        raise BoundaryViolation("git.ref_invalid", "Git ref is not canonical and safe")
    return value


def normalize_unreal_asset(value: str) -> str:
    if not value.startswith("/Game/") or ".." in value or "\\" in value:
        raise BoundaryViolation(
            "unreal.asset_invalid", "Unreal asset target must be a canonical /Game/ path"
        )
    return value
