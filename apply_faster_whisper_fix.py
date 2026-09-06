"""Build-time multilingual-window correction for exact faster-whisper 1.2.1.

Classify the leading ten seconds, rather than letting a later language dominate
the window. In multilingual word-timed decoding, revisit audio after the last
recognised word instead of assuming an end token means the remaining audio is
silent. Explicit-language decoding retains upstream behaviour.
"""
import argparse
import hashlib
import importlib.util
from pathlib import Path

ORIGINAL_SHA256 = '5d5ffb00018561d3d529b2c72e1d9f5fff055bea725f3cccc7c6c67f5cc8ffe4'
PATCHED_SHA256 = '9cfafd5a1e9f070b2a40c1d70533b2eb59e2f78b6a52f51d050f542a94f8abf9'
REPLACEMENTS = (
    ('import numpy as np\n', 'import numpy as np\nSUBGEN_LANGUAGE_WINDOW_FIX = 2\n'),
    ('                    features=features[..., seek:],\n',
     '                    features=features[..., seek:seek+1000] if multilingual else features[..., seek:],\n'),
    ('            if options.multilingual:\n'
     '                results = self.model.detect_language(encoder_output)\n',
     '            if options.multilingual:\n'
     '                language_encoder = self.encode(pad_or_trim(segment[:, :min(segment_size, 1000)]))\n'
     '                results = self.model.detect_language(language_encoder)\n'
     '                del language_encoder\n'),
    ('                if not single_timestamp_ending:\n',
     '                if not single_timestamp_ending or options.multilingual:\n'),
)


def apply_fix(path: Path) -> bool:
    source = path.read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    if digest == PATCHED_SHA256:
        return False
    if digest != ORIGINAL_SHA256:
        raise ValueError('Unexpected faster-whisper source hash; review before building')
    patched = source
    for old, new in REPLACEMENTS:
        old, new = old.encode('utf8'), new.encode('utf8')
        if patched.count(old) != 1:
            raise ValueError('Expected exactly one faster-whisper correction block')
        patched = patched.replace(old, new, 1)
    if hashlib.sha256(patched).hexdigest() != PATCHED_SHA256:
        raise ValueError('Unexpected patched faster-whisper hash; source unchanged')
    path.write_bytes(patched)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-path', type=Path)
    args = parser.parse_args()
    path = args.source_path
    if path is None:
        spec = importlib.util.find_spec('faster_whisper')
        if spec is None or spec.origin is None:
            raise RuntimeError('The pinned faster-whisper runtime is not installed')
        path = Path(spec.origin).with_name('transcribe.py')
    changed = apply_fix(path)
    print('faster-whisper language-window correction ' + ('applied' if changed else 'already present'))


if __name__ == '__main__':
    main()
