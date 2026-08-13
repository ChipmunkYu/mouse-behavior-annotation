"""Frozen canonical encoding shared by runtime and migrations."""
import hashlib
import json

def canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()

def canonical_digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()

def canonical_rows_digest(rows) -> str:
    digest = hashlib.sha256(b"[")
    first = True
    for row in rows:
        if not first:
            digest.update(b",")
        digest.update(canonical_bytes(row)); first = False
    digest.update(b"]")
    return digest.hexdigest()

def validate_pose_metadata(names, edges) -> tuple[list[str], list[list[int]]]:
    """Validate frozen pose semantics shared by runtime and migration 0010."""
    if (not isinstance(names, list) or not names
            or any(not isinstance(name, str) or not name or name != name.strip() for name in names)
            or len(set(names)) != len(names)):
        raise ValueError("keypoint_names must be a non-empty unique controlled string array")
    if not isinstance(edges, list):
        raise ValueError("skeleton_edges must be an array")
    seen = set()
    for edge in edges:
        if (not isinstance(edge, list) or len(edge) != 2
                or any(isinstance(index, bool) or not isinstance(index, int) for index in edge)):
            raise ValueError("each skeleton edge must contain exactly two non-bool integers")
        left, right = edge
        if left < 0 or right < 0 or left >= len(names) or right >= len(names):
            raise ValueError("skeleton edge index is out of range")
        if left == right:
            raise ValueError("skeleton self loops are forbidden")
        canonical = tuple(sorted((left, right)))
        if canonical in seen:
            raise ValueError("duplicate skeleton edges are forbidden")
        seen.add(canonical)
    return list(names), [list(edge) for edge in edges]
