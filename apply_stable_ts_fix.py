"""Build-time ordering correction for the pinned stable-ts 2.19.1 runtime.

Normalize away wordless segments before the existing word-order checks. In a
mixed result they otherwise select segment-only validation, then disappear
after validation, leaving invalid interior word timestamps unchecked.
"""

import argparse
import hashlib
import importlib.util
from pathlib import Path


ORIGINAL_SHA256 = "d9ec5d003e2b2d2377753add7dfee0533d7c7c40e4aceb88c895bd0113f28e20"
PATCHED_SHA256 = "b38db97510a8093fe0c8b2a1ba5e830496dcfd0a1f6de389abeaafddd95a45bb"
ORIGINAL_BLOCK = (
    "        self._forced_order = force_order\n"
    "        if self._forced_order:\n"
    "            self.force_order()\n"
    "        self.raise_for_unsorted(check_sorted, show_unsorted)\n"
    "        self.remove_no_word_segments(any(seg.has_words for seg in self.segments))\n"
)
PATCHED_BLOCK = (
    "        self._forced_order = force_order\n"
    "        self.remove_no_word_segments(any(seg.has_words for seg in self.segments))\n"
    "        if self._forced_order:\n"
    "            self.force_order()\n"
    "        self.raise_for_unsorted(check_sorted, show_unsorted)\n"
)


def apply_fix(result_path: Path) -> bool:
    """Patch only the exact reviewed source; return False when already applied."""
    source = result_path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest == PATCHED_SHA256:
        return False
    if digest != ORIGINAL_SHA256:
        raise ValueError("Unexpected stable-ts result.py hash; review the runtime before building")
    # The pinned source uses CRLF. Preserve it, including the rest of the file.
    old = ORIGINAL_BLOCK.replace("\n", "\r\n").encode("utf-8")
    new = PATCHED_BLOCK.replace("\n", "\r\n").encode("utf-8")
    if source.count(old) != 1:
        raise ValueError("Expected exactly one stable-ts constructor ordering block")
    patched = source.replace(old, new, 1)
    if hashlib.sha256(patched).hexdigest() != PATCHED_SHA256:
        raise ValueError("Unexpected patched stable-ts hash; source was not changed")
    result_path.write_bytes(patched)
    return True


def installed_result_path() -> Path:
    spec = importlib.util.find_spec("stable_whisper")
    if spec is None or spec.origin is None:
        raise RuntimeError("The pinned stable-ts runtime is not installed")
    return Path(spec.origin).with_name("result.py")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-path", type=Path, help="Disposable source copy for verification")
    args = parser.parse_args()
    changed = apply_fix(args.result_path or installed_result_path())
    print("stable-ts timing-order correction " + ("applied" if changed else "already present"))


if __name__ == "__main__":
    main()
