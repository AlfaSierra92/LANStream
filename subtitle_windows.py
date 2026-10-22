"""Build a tiny subtitle-only Matroska window in memory for FFmpeg conversion.

The remote reader is FFprobe, which stops on all-stream timestamps and explicit
packet counts. FFmpeg never reopens the full film to wait for a distant cue.
"""
import math
import json
import codecs
import subprocess
import threading
import time


def capture_packets(runtime, command, count, timeout, start=0):
    """Stop on the last printed packet, before FFprobe's extra lookahead read."""
    process = runtime.popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=65536)
    done = threading.Event()
    packets, failures = [], []
    deadline = time.monotonic() + timeout

    def read():
        total = 0
        buffer = ''
        started = False
        decoder = json.JSONDecoder()
        utf8 = codecs.getincrementaldecoder('utf-8')()
        try:
            while chunk := process.stdout.read1(4096):
                total += len(chunk)
                if total > 8 * 1024 * 1024:
                    raise ValueError('Subtitle packets are too large')
                buffer += utf8.decode(chunk)
                if not started:
                    marker = buffer.find('"packets"')
                    bracket = buffer.find('[', marker) if marker >= 0 else -1
                    if bracket < 0:
                        continue
                    buffer = buffer[bracket + 1:]
                    started = True
                while True:
                    buffer = buffer.lstrip(' \r\n\t,')
                    if buffer.startswith(']'):
                        return
                    try:
                        row, end = decoder.raw_decode(buffer)
                    except json.JSONDecodeError:
                        break
                    buffer = buffer[end:]
                    if 'data' in row:
                        pts = float(row.get('pts_time', 'nan'))
                        length = float(row.get('duration_time', 0) or 0)
                        if math.isfinite(pts) and pts + max(0, length) <= start:
                            continue
                        packets.append(row)
                        if len(packets) >= count:
                            return
        except (ValueError, OSError) as exc:
            failures.append(exc)
        finally:
            done.set()

    def drain():
        try:
            while process.stderr.read(4096):
                pass
        except (ValueError, OSError):
            pass

    reader = threading.Thread(target=read, name='subtitle-packets', daemon=True)
    errors = threading.Thread(target=drain, name='subtitle-errors', daemon=True)
    reader.start()
    errors.start()
    try:
        if not done.wait(max(0, deadline - time.monotonic())):
            raise subprocess.TimeoutExpired(command, timeout)
        if failures:
            raise failures[0]
        if len(packets) < count:
            process.wait(timeout=max(0, deadline - time.monotonic()))
            if process.returncode:
                raise ValueError('Unable to read subtitle packets')
        return packets
    finally:
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        finally:
            reader.join(timeout=2)
            errors.join(timeout=2)
            for pipe in (process.stdout, process.stderr):
                if pipe:
                    pipe.close()
            runtime.release(process)


def unhex(text):
    output = bytearray()
    for line in text.splitlines():
        if ':' not in line:
            continue
        hexpart = line.split(':', 1)[1].lstrip().split('  ', 1)[0]
        output.extend(bytes.fromhex(hexpart))
        if len(output) > 2 * 1024 * 1024:
            raise ValueError('Subtitle data is too large')
    return bytes(output)


def matroska_payload(data, expected, offset):
    """Validate the EBML Block header preceding FFprobe's packet position."""
    def vint(at):
        first = data[at]
        width = next(n for n in range(1, 9) if first & (1 << (8 - n)))
        value = int.from_bytes(data[at:at + width], 'big') & ((1 << (7 * width)) - 1)
        return value, width
    for identifier in range(max(0, offset - 9), offset):
        try:
            if data[identifier] not in (0xA1, 0xA3):
                continue
            size, width = vint(identifier + 1)
            if identifier + 1 + width != offset:
                continue
            _, track_width = vint(offset)
            header = track_width + 3
            beginning = offset + header
            if size == expected + header and not data[beginning - 1] & 6:
                payload = data[beginning:beginning + expected]
                if len(payload) == expected:
                    return payload
        except (IndexError, StopIteration):
            pass
    raise ValueError('Subtitle Block uses unsupported lacing or compression')


def element(identifier, data):
    identifier = identifier.to_bytes((identifier.bit_length() + 7) // 8, 'big')
    width = next(n for n in range(1, 9) if len(data) < (1 << (7 * n)) - 1)
    size = ((1 << (7 * width)) | len(data)).to_bytes(width, 'big')
    return identifier + size + data


def number(identifier, value):
    return element(identifier, value.to_bytes(max(1, (value.bit_length() + 7) // 8), 'big'))


def subtitle_container(info, packets, start, duration):
    codec = info.get('codec_name', '')
    ass = codec in {'ass', 'ssa'}
    header = element(0x1A45DFA3,
        number(0x4286, 1) + number(0x42F7, 1) + number(0x42F2, 4) + number(0x42F3, 8)
        + element(0x4282, b'matroska') + number(0x4287, 4) + number(0x4285, 2))
    track = number(0xD7, 1) + number(0x73C5, 1) + number(0x83, 17) + number(0x9C, 0)
    track += element(0x86, b'S_TEXT/ASS' if ass else b'S_TEXT/UTF8')
    if ass:
        track += element(0x63A2, unhex(info.get('extradata', '')))
    segment = (element(0x1549A966, number(0x2AD7B1, 1000000)
                       + element(0x4D80, b'LANStream') + element(0x5741, b'LANStream'))
               + element(0x1654AE6B, element(0xAE, track)))
    chunks = []
    total = 0
    for index, packet in enumerate(packets):
        pts = float(packet.get('pts_time', 'nan')) - start
        if not math.isfinite(pts) or pts >= duration:
            continue
        length = float(packet.get('duration_time', 0) or 0)
        if not math.isfinite(length) or length <= 0:
            next_pts = float(packets[index + 1].get('pts_time', start + duration)) - start if index + 1 < len(packets) else duration
            length = max(0, next_pts - pts)
        # A cue may remain visible beyond this packet window. Preserve its
        # known end time; later windows may contain no new packet to extend it.
        end = pts + length
        if end <= 0:
            continue
        payload = packet.get('payload')
        if payload is None:
            payload = unhex(packet.get('data', ''))
        if codec == 'mov_text':
            if len(payload) < 2:
                continue
            size = int.from_bytes(payload[:2], 'big')
            payload = payload[2:2 + size]
            if payload.startswith((b'\xff\xfe', b'\xfe\xff')):
                payload = payload.decode('utf-16').encode('utf-8')
        if not payload:
            continue
        total += len(payload)
        if total > 2 * 1024 * 1024:
            raise ValueError('Subtitle window is too large')
        begin_ms = max(0, round(pts * 1000))
        duration_ms = max(1, round(end * 1000) - begin_ms)
        block = element(0xA1, b'\x81\x00\x00\x00' + payload) + number(0x9B, duration_ms)
        chunks.append(element(0x1F43B675, number(0xE7, begin_ms) + element(0xA0, block)))
    if not chunks:
        return None
    # Unknown Segment size avoids a seekable output or any temporary file.
    return header + b'\x18\x53\x80\x67\x01\xff\xff\xff\xff\xff\xff\xff' + segment + b''.join(chunks)
