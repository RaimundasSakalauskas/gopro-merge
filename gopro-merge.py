#!/usr/bin/env python3
"""Losslessly merge GoPro chapter files (HERO .MP4, MAX/MAX2 .360) into a single file.

Works on the MP4 box tree directly instead of going through ffmpeg: ffmpeg's concat demuxer fails on the gpmd
track's edit-list timing, and its muxer drops the moov/udta GPMF box and the zeroed matrix of the .360 second video
track, which GoPro Player relies on. Here the first chapter's moov is kept verbatim and only the per-track sample
tables, durations and edit lists are rebuilt, so every camera-specific box survives.

Usage:
    gopro-merge.py GX010042.MP4             # finds GX02/GX03... siblings, asks which to include
    gopro-merge.py ride.360                 # finds ride.part2.360, ride.part3.360...
    gopro-merge.py a.mp4 b.mp4 c.mp4        # merges exactly these, sorted by chapter/part number
"""

import argparse
import os
import re
import shutil
import struct
import subprocess
import sys

CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts'}
SUPPORTED_STBL = {b'stsd', b'stts', b'ctts', b'stsz', b'stsc', b'stco', b'co64', b'stss', b'sdtp', b'sgpd', b'sbgp'}
BTRT = re.compile(rb'\x00\x00\x00\x14btrt.{12}', re.DOTALL)
EXTENSIONS = {'.360', '.mp4'}
GOPRO_NAME = re.compile(r'^(G[A-Z])(\d{2})(\d{4})$', re.IGNORECASE)
# Cameras before HERO6 name the first chapter GOPR1234 and the following ones GP011234, GP021234...
GOPRO_LEGACY_FIRST = re.compile(r'^GOPR(\d{4})$', re.IGNORECASE)
PART_SUFFIX = re.compile(r'^(.+)\.part(\d+)$', re.IGNORECASE)
COPY_BUFFER = 16 * 1024 * 1024


class MergeError(Exception):
    pass


# MARK: - Box tree

class Box:
    def __init__(self, type, content):
        self.type = type
        self.content = content  # list[Box] for containers, bytes otherwise

    def child(self, type):
        return next((c for c in self.content if c.type == type), None)

    def children(self, type):
        return [c for c in self.content if c.type == type]

    def path(self, *types):
        box = self
        for t in types:
            box = box.child(t) if box else None
        return box

    def serialize(self):
        body = b''.join(c.serialize() for c in self.content) if isinstance(self.content, list) else self.content
        if len(body) + 8 > 0xFFFFFFFF:
            return struct.pack('>I4sQ', 1, self.type, len(body) + 16) + body
        return struct.pack('>I4s', len(body) + 8, self.type) + body


def parse_boxes(buf):
    boxes, off = [], 0
    while off + 8 <= len(buf):
        size, typ = struct.unpack_from('>I4s', buf, off)
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack_from('>Q', buf, off + 8)[0], 16
        elif size == 0:
            size = len(buf) - off
        body = buf[off + hdr:off + size]
        boxes.append(Box(typ, parse_boxes(body) if typ in CONTAINERS else body))
        off += size
    return boxes


def top_level_boxes(f, file_size):
    off = 0
    while off < file_size:
        f.seek(off)
        size, typ = struct.unpack('>I4s', f.read(8))
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack('>Q', f.read(8))[0], 16
        elif size == 0:
            size = file_size - off
        yield typ, off, hdr, size
        off += size


# MARK: - Sample tables

def read_full(body):
    return body[0], body[1:4], body[4:]


def flatten(rows):
    return [value for row in rows for value in row]


def full_box(type, version, flags, payload):
    return Box(type, bytes([version]) + flags + payload)


def read_descriptor_header(data, pos):
    tag, length = data[pos], 0
    pos += 1
    for _ in range(4):
        byte = data[pos]
        pos += 1
        length = (length << 7) | (byte & 0x7F)
        if not byte & 0x80:
            break
    return tag, length, pos


def without_bitrate_stats(stsd):
    """The sample description minus the per-file bitrate statistics, which differ between chapters of one recording."""
    data = bytearray(BTRT.sub(b'', stsd))
    at = data.find(b'esds')
    if at < 0:
        return bytes(data)
    tag, _, pos = read_descriptor_header(data, at + 8)
    if tag != 0x03:
        return bytes(data)
    flags = data[pos + 2]
    pos += 3
    if flags & 0x80:
        pos += 2
    if flags & 0x40:
        pos += 1 + data[pos]
    if flags & 0x20:
        pos += 2
    tag, _, pos = read_descriptor_header(data, pos)
    if tag == 0x04:
        data[pos + 2:pos + 13] = bytes(11)  # bufferSizeDB, maxBitrate, avgBitrate
    return bytes(data)


def sbgp_key(body):
    # grouping_type, plus grouping_type_parameter in version 1
    return body[4:12] if body[0] == 1 else body[4:8]


def read_entries(body, fmt, header_fmt='>I'):
    _, _, rest = read_full(body)
    count = struct.unpack_from(header_fmt, rest)[-1]
    start = struct.calcsize(header_fmt)
    step = struct.calcsize(fmt)
    return [struct.unpack_from(fmt, rest, start + i * step) for i in range(count)]


class Track:
    def __init__(self, trak, movie_timescale):
        self.trak = trak
        mdia = trak.child(b'mdia')
        self.handler = mdia.child(b'hdlr').content[8:12]
        stbl = mdia.path(b'minf', b'stbl')
        unsupported = {c.type for c in stbl.content} - SUPPORTED_STBL
        if unsupported:
            raise MergeError(f'unsupported sample table boxes {sorted(unsupported)}')

        self.stsd = stbl.child(b'stsd').content
        self.stsd_settings = without_bitrate_stats(self.stsd)
        self.timescale, self.media_duration = self._mdhd(mdia.child(b'mdhd').content)
        self.stts = read_entries(stbl.child(b'stts').content, '>II')

        ctts = stbl.child(b'ctts')
        self.ctts_version = ctts.content[0] if ctts else 0
        self.ctts = read_entries(ctts.content, '>Ii' if self.ctts_version else '>II') if ctts else None

        _, _, rest = read_full(stbl.child(b'stsz').content)
        self.sample_size, self.sample_count = struct.unpack_from('>II', rest)
        self.sizes = None if self.sample_size else list(struct.unpack_from(f'>{self.sample_count}I', rest, 8))

        self.stsc = read_entries(stbl.child(b'stsc').content, '>III')
        if stbl.child(b'co64'):
            self.chunk_offsets = [e[0] for e in read_entries(stbl.child(b'co64').content, '>Q')]
        else:
            self.chunk_offsets = [e[0] for e in read_entries(stbl.child(b'stco').content, '>I')]

        stss = stbl.child(b'stss')
        self.sync = [e[0] for e in read_entries(stss.content, '>I')] if stss else None
        sdtp = stbl.child(b'sdtp')
        self.sdtp = read_full(sdtp.content)[2] if sdtp else None

        self.sgpd = sorted(c.content for c in stbl.children(b'sgpd'))
        self.sbgp = {}
        for box in stbl.children(b'sbgp'):
            key = sbgp_key(box.content)
            count = struct.unpack_from('>I', box.content, 4 + len(key))[0]
            self.sbgp[key] = (box.content[0], [struct.unpack_from('>II', box.content, 8 + len(key) + 8 * i) for i in range(count)])

        self.edits = self._edits(trak.path(b'edts', b'elst'), movie_timescale)

    @property
    def presentation_end(self):
        """Media time at which the last frame stops showing; past media_duration when B-frames shift frames later."""
        if not self.ctts:
            return self.media_duration
        offsets = (offset for count, offset in self.ctts for _ in range(count))
        end = dts = 0
        for count, delta in self.stts:
            for _ in range(count):
                end = max(end, dts + next(offsets, 0) + delta)
                dts += delta
        return end

    @staticmethod
    def _mdhd(body):
        version, _, rest = read_full(body)
        if version == 1:
            return struct.unpack_from('>IQ', rest, 16)
        return struct.unpack_from('>II', rest, 8)

    def _edits(self, elst, movie_timescale):
        if not elst:
            duration = self.media_duration * movie_timescale // self.timescale
            return [(duration, 0, 0x00010000)]
        version = elst.content[0]
        fmt = '>QqI' if version == 1 else '>IiI'
        return read_entries(elst.content, fmt)


# MARK: - Chapters

class Chapter:
    def __init__(self, path):
        self.path = path
        self.size = os.path.getsize(path)
        mdats, moov, self.ftyp = [], None, None
        with open(path, 'rb') as f:
            for typ, off, hdr, size in top_level_boxes(f, self.size):
                if typ == b'ftyp':
                    f.seek(off)
                    self.ftyp = f.read(size)
                elif typ == b'moov':
                    f.seek(off + hdr)
                    moov = Box(b'moov', parse_boxes(f.read(size - hdr)))
                elif typ == b'mdat':
                    mdats.append((off + hdr, off + size))
        if not moov or len(mdats) != 1:
            raise MergeError(f'{path}: expected one moov and one mdat box')
        self.moov = moov
        self.mdat_start, self.mdat_end = mdats[0]
        self.movie_timescale = struct.unpack_from('>I', moov.child(b'mvhd').content, 20 if moov.child(b'mvhd').content[0] == 1 else 12)[0]
        try:
            self.tracks = [Track(t, self.movie_timescale) for t in moov.children(b'trak')]
        except MergeError as e:
            raise MergeError(f'{path}: {e}')
        for track in self.tracks:
            if any(not self.mdat_start <= o < self.mdat_end for o in track.chunk_offsets):
                raise MergeError(f'{path}: sample data outside the mdat box')

    @property
    def duration_seconds(self):
        return max(sum(e[0] for e in t.edits) for t in self.tracks) / self.movie_timescale


def check_compatible(chapters):
    first = chapters[0]
    for ch in chapters[1:]:
        if ch.movie_timescale != first.movie_timescale or len(ch.tracks) != len(first.tracks):
            raise MergeError(f'{ch.path} has a different track layout than {first.path}')
        for a, b in zip(first.tracks, ch.tracks):
            if (a.handler, a.timescale, a.stsd_settings, a.sgpd) != (b.handler, b.timescale, b.stsd_settings, b.sgpd):
                raise MergeError(f'{ch.path} was recorded with different settings than {first.path}')


# MARK: - Merge

def merged_trak(index, chapters, data_bases):
    tracks = [ch.tracks[index] for ch in chapters]
    stts, ctts, sizes, stsc, offsets, sync, sdtp, edits = [], [], [], [], [], [], b'', []
    samples = chunks = media_duration = 0
    uniform = tracks[0].sample_size if len({t.sample_size for t in tracks}) == 1 else 0
    any_sync = any(t.sync is not None for t in tracks)
    groups = {key: (version, []) for t in tracks for key, (version, _) in t.sbgp.items()}

    for ch, t, base in zip(chapters, tracks, data_bases):
        for key, (_, merged) in groups.items():
            entries = t.sbgp.get(key, (0, []))[1]
            # Samples past the last entry belong to no group; make that explicit before the next chapter's entries.
            uncovered = t.sample_count - sum(count for count, _ in entries)
            for count, index in entries + ([(uncovered, 0)] if uncovered > 0 else []):
                if merged and merged[-1][1] == index:
                    merged[-1] = (merged[-1][0] + count, index)
                else:
                    merged.append((count, index))
        for count, delta in t.stts:
            if stts and stts[-1][1] == delta:
                stts[-1] = (stts[-1][0] + count, delta)
            else:
                stts.append((count, delta))
        if t.ctts is not None:
            ctts.extend(t.ctts)
        elif any(o.ctts is not None for o in tracks):
            ctts.append((t.sample_count, 0))
        if not uniform:
            sizes.extend(t.sizes if t.sizes is not None else [t.sample_size] * t.sample_count)
        stsc.extend((first + chunks, per_chunk, desc) for first, per_chunk, desc in t.stsc)
        offsets.extend(o - ch.mdat_start + base for o in t.chunk_offsets)
        if any_sync:
            sync.extend(s + samples for s in (t.sync if t.sync is not None else range(1, t.sample_count + 1)))
        if t.sdtp is not None:
            sdtp += t.sdtp
        # Each chapter keeps its own edit (GoPro hides a short pre-roll at the start of every chapter), shifted to
        # where that chapter's media now starts.
        chapter_edits = list(t.edits)
        dur, mt, rate = chapter_edits[-1]
        if ch is not chapters[-1] and mt >= 0:
            # A writer that rounds the edit down to whole movie ticks ends it inside the last frame. Harmless at the
            # end of a file, but between chapters players then drop that frame, so round it up instead.
            visible_media = (t.presentation_end - mt) * ch.movie_timescale / t.timescale
            if 0 < visible_media - dur < 1:
                chapter_edits[-1] = (dur + 1, mt, rate)
        edits.extend((dur, mt + media_duration if mt >= 0 else -1, rate) for dur, mt, rate in chapter_edits)
        samples += t.sample_count
        chunks += len(t.chunk_offsets)
        media_duration += t.media_duration

    trak = tracks[0].trak
    mdia = trak.child(b'mdia')
    stbl = mdia.path(b'minf', b'stbl')
    tables = {
        b'stts': full_box(b'stts', 0, b'\0\0\0', struct.pack(f'>I{len(stts) * 2}I', len(stts), *flatten(stts))),
        b'stsz': full_box(b'stsz', 0, b'\0\0\0', struct.pack('>II', uniform, samples) + (b'' if uniform else struct.pack(f'>{len(sizes)}I', *sizes))),
        b'stsc': full_box(b'stsc', 0, b'\0\0\0', struct.pack(f'>I{len(stsc) * 3}I', len(stsc), *flatten(stsc))),
        b'co64': full_box(b'co64', 0, b'\0\0\0', struct.pack(f'>I{len(offsets)}Q', len(offsets), *offsets)),
    }
    if ctts:
        version = tracks[0].ctts_version
        tables[b'ctts'] = full_box(b'ctts', version, b'\0\0\0', struct.pack('>I' + ('Ii' if version else 'II') * len(ctts), len(ctts), *flatten(ctts)))
    if any_sync:
        tables[b'stss'] = full_box(b'stss', 0, b'\0\0\0', struct.pack(f'>I{len(sync)}I', len(sync), *sync))
    if sdtp:
        tables[b'sdtp'] = full_box(b'sdtp', 0, b'\0\0\0', sdtp)

    sbgp_boxes = {
        key: full_box(b'sbgp', version, b'\0\0\0', key + struct.pack(f'>I{len(merged) * 2}I', len(merged), *flatten(merged)))
        for key, (version, merged) in groups.items()
    }

    new_stbl = []
    for box in stbl.content:
        if box.type == b'sbgp':
            new_stbl.append(sbgp_boxes.pop(sbgp_key(box.content)))
            continue
        key = b'co64' if box.type in (b'stco', b'co64') else box.type
        new_stbl.append(tables.get(key, box))
    stbl.content = new_stbl + list(sbgp_boxes.values())

    track_duration = sum(e[0] for e in edits)
    elst = trak.path(b'edts', b'elst')
    elst_payload = struct.pack('>I', len(edits)) + b''.join(struct.pack('>QqI', *e) for e in edits)
    if elst:
        elst.content = bytes([1]) + elst.content[1:4] + elst_payload
    else:
        trak.content.insert(1, Box(b'edts', [full_box(b'elst', 1, b'\0\0\0', elst_payload)]))

    set_duration(mdia.child(b'mdhd'), media_duration)
    set_duration(trak.child(b'tkhd'), track_duration)
    return trak, track_duration


def set_duration(box, duration):
    body = bytearray(box.content)
    version = body[0]
    # mvhd/mdhd: times, timescale, duration. tkhd: times, track_ID, reserved, duration.
    offset = (4 + 16 + 4) if version == 1 else (4 + 8 + 4)
    if box.type == b'tkhd':
        offset += 4
    if version == 1:
        struct.pack_into('>Q', body, offset, duration)
    elif duration <= 0xFFFFFFFF:
        struct.pack_into('>I', body, offset, duration)
    else:
        raise MergeError('merged video is too long for this file layout')
    box.content = bytes(body)


def merge(chapters, out_path):
    check_compatible(chapters)
    first = chapters[0]
    tmp_path = out_path + '.part'
    with open(tmp_path, 'wb') as out:
        out.write(first.ftyp)
        mdat_header = out.tell()
        out.write(struct.pack('>I4sQ', 1, b'mdat', 0))
        data_bases = []
        for i, ch in enumerate(chapters, 1):
            print(f'  copying {i}/{len(chapters)}: {os.path.basename(ch.path)}', flush=True)
            data_bases.append(out.tell())
            with open(ch.path, 'rb') as src:
                src.seek(ch.mdat_start)
                remaining = ch.mdat_end - ch.mdat_start
                while remaining:
                    chunk = src.read(min(COPY_BUFFER, remaining))
                    out.write(chunk)
                    remaining -= len(chunk)
        mdat_end = out.tell()
        out.seek(mdat_header + 8)
        out.write(struct.pack('>Q', mdat_end - mdat_header))
        out.seek(mdat_end)

        moov = first.moov
        movie_duration = 0
        for i in range(len(first.tracks)):
            _, duration = merged_trak(i, chapters, data_bases)
            movie_duration = max(movie_duration, duration)
        set_duration(moov.child(b'mvhd'), movie_duration)
        out.write(moov.serialize())
    os.replace(tmp_path, out_path)


# MARK: - CLI

# Each naming scheme maps a file name to (series, chapter number); files of one recording share the series.

def part_id(name):
    stem, ext = os.path.splitext(name)
    if ext.lower() not in EXTENSIONS:
        return None
    match = PART_SUFFIX.match(stem)
    if match:
        return (match.group(1), ext.lower()), int(match.group(2))
    return (stem, ext.lower()), 1


def gopro_id(name):
    stem, ext = os.path.splitext(name)
    if ext.lower() not in EXTENSIONS:
        return None
    legacy = GOPRO_LEGACY_FIRST.match(stem)
    if legacy:
        return ('GP', legacy.group(1), ext.lower()), 0
    match = GOPRO_NAME.match(stem)
    if match:
        return (match.group(1).upper(), match.group(3), ext.lower()), int(match.group(2))
    return None


def find_chapters(path):
    directory, name = os.path.split(os.path.abspath(path))
    names = os.listdir(directory)
    for scheme in (part_id, gopro_id):
        own = scheme(name)
        if not own:
            continue
        found = sorted((sid[1], os.path.join(directory, n)) for n in names for sid in [scheme(n)] if sid and sid[0] == own[0])
        if len(found) > 1:
            return [p for _, p in found]
    return [path]


def chapter_key(path):
    name = os.path.basename(path)
    stem = os.path.splitext(name)[0]
    key = None if PART_SUFFIX.match(stem) else gopro_id(name)
    return key or part_id(name) or ((name, ''), 0)


def parse_selection(text, count):
    picked = []
    for part in text.replace(' ', '').split(','):
        if '-' in part:
            lo, hi = part.split('-')
            picked.extend(range(int(lo), int(hi) + 1))
        elif part:
            picked.append(int(part))
    if not picked or any(not 1 <= n <= count for n in picked):
        raise ValueError
    return list(dict.fromkeys(picked))


def choose(paths):
    if not sys.stdin.isatty():
        return paths
    while True:
        answer = input('Chapters to merge [Enter = all, e.g. 1-3 or 1,2,4]: ').strip()
        if not answer:
            return paths
        try:
            return [paths[n - 1] for n in parse_selection(answer, len(paths))]
        except ValueError:
            print(f'  Enter numbers between 1 and {len(paths)}.')


def osascript(*lines, arg):
    subprocess.run(['osascript', '-e', 'on run argv', *(e for line in lines for e in ('-e', line)), '-e', 'end run', arg])


def notify(message):
    osascript('display notification (item 1 of argv) with title "GoPro merge"', arg=message)


def main(args):
    report = notify if args.service else print
    paths = sorted(args.files, key=chapter_key) if len(args.files) > 1 else find_chapters(args.files[0])
    try:
        chapters = [Chapter(p) for p in paths]
    except (MergeError, OSError, struct.error) as e:
        sys.exit(f'error: {e}')

    for i, ch in enumerate(chapters, 1):
        print(f'  {i}. {os.path.basename(ch.path)}  {ch.duration_seconds / 60:5.1f} min  {ch.size / 1e9:5.2f} GB')
    if len(args.files) == 1 and len(chapters) > 1 and not args.yes:
        selected = choose(paths)
        chapters = [ch for p in selected for ch in chapters if ch.path == p]
    if len(chapters) < 2:
        sys.exit('Nothing to merge: only one chapter selected or found.')

    stem, ext = os.path.splitext(chapters[0].path)
    out_path = args.output or stem + '_merged' + ext
    if os.path.exists(out_path):
        sys.exit(f'error: {out_path} already exists')
    needed = sum(ch.size for ch in chapters)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(out_path))).free
    if needed > free:
        sys.exit(f'error: needs {needed / 1e9:.1f} GB free, only {free / 1e9:.1f} GB available')

    report(f'Merging {len(chapters)} chapters -> {out_path}')
    try:
        merge(chapters, out_path)
    except (MergeError, OSError) as e:
        if os.path.exists(out_path + '.part'):
            os.remove(out_path + '.part')
        sys.exit(f'error: {e}')
    total = sum(ch.duration_seconds for ch in chapters)
    report(f'Done: {os.path.basename(out_path)}, {total / 60:.1f} min')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Losslessly merge GoPro chapter files (.MP4 and .360).')
    parser.add_argument('files', nargs='+', help='first chapter (siblings are found automatically) or several chapters')
    parser.add_argument('-o', '--output', help='output path (default: <first>_merged.<ext> next to the first file)')
    parser.add_argument('-y', '--yes', action='store_true', help="don't ask, merge every chapter found")
    parser.add_argument('--service', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        main(args)
    except SystemExit as e:
        if not (args.service and isinstance(e.code, str)):
            raise
        # Exit cleanly after our own alert, otherwise Automator stacks its generic "action failed" alert on top.
        osascript('display alert "GoPro merge failed" message (item 1 of argv) as critical', arg=e.code)
