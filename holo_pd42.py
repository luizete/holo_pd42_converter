"""
Encoder/decoder for the Holoscope PD42 (42 cm) holographic LED fan.

File format (named .mp4, but the content is a proprietary binary format):

  [4 bytes]           header 01 00 00 00
  [N x 141120 bytes]  frames
  [8 bytes]           trailer: 5A 91 | N (uint32 big-endian) | XOR of the 4 bytes of N | F5
                      (stock files always have N = 12*k + 11, e.g. 95, 119, 191)

Each frame = 245 rows (angular positions covering 180 degrees) x 576 bytes.
The propeller has 2 opposite arms of 128 RGB LEDs each; every row is one "diameter".
Each row = 6 bit planes (plane 0 = MSB -> 6 bits per channel) x 96 bytes.
Each plane = 64 groups of 12 bits: [R nibble][G nibble][B nibble].
Inside each nibble, bit 3 (MSB) .. bit 0 address 4 segments of 64 LEDs:
  seg0 = arm A, LEDs 64..127     seg1 = arm A, LEDs 0..63
  seg2 = arm B, LEDs 0..63       seg3 = arm B, LEDs 64..127
LED 0 of each arm = outer edge; LED 127 = center.
On row a, arm B points at angle pi*a/245 (x to the LEFT, y down, seen from the
front); arm A points the opposite way (+pi).
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys

import numpy as np
from PIL import Image, ImageOps, ImageSequence

ROWS, LEDS, SEG, PLANES = 245, 128, 64, 6
FRAME_BYTES = ROWS * PLANES * SEG * 3 * 4 // 8  # 141120
HEADER = b"\x01\x00\x00\x00"

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
SIZE = 1024  # internal square resolution used for sampling


def trailer(n):
    c = struct.pack(">I", n)
    return b"\x5a\x91" + c + bytes([c[0] ^ c[1] ^ c[2] ^ c[3]]) + b"\xf5"


def sample_coords(size, mirror=False, rotate_deg=0.0):
    """Pixel coordinates (x, y) for every (row, arm, LED)."""
    a = np.arange(ROWS)[:, None, None]
    arm = np.arange(2)[None, :, None]          # 0 = arm B, 1 = arm A
    i = np.arange(LEDS)[None, None, :]
    theta = np.pi * a / ROWS + np.pi * arm + np.deg2rad(rotate_deg)
    rho = (LEDS - 1 - i + 0.5) / LEDS * (size / 2)
    x = size / 2 - rho * np.cos(theta)  # seen from the front, the angle grows to the left
    y = size / 2 + rho * np.sin(theta)
    if mirror:
        x = size - x
    return x, y


def bilinear(img, x, y):
    h, w, _ = img.shape
    x = np.clip(x - 0.5, 0, w - 1.001)
    y = np.clip(y - 0.5, 0, h - 1.001)
    x0 = x.astype(int); y0 = y.astype(int)
    fx = (x - x0)[..., None]; fy = (y - y0)[..., None]
    p = img.astype(np.float32)
    return (p[y0, x0] * (1 - fx) * (1 - fy) + p[y0, x0 + 1] * fx * (1 - fy)
            + p[y0 + 1, x0] * (1 - fx) * fy + p[y0 + 1, x0 + 1] * fx * fy)


def leds_to_frame(leds):
    """leds: (245, 2, 128, 3) uint8 with values 0..63 -> frame bytes."""
    B, A = leds[:, 0], leds[:, 1]
    seg = np.stack([A[:, 64:], A[:, :64], B[:, :64], B[:, 64:]], axis=-1)  # (245,64,3,4)
    planes = np.stack([(seg >> (5 - k)) & 1 for k in range(PLANES)], axis=1)  # (245,6,64,3,4)
    return np.packbits(planes.astype(np.uint8).reshape(-1)).tobytes()


def frame_to_leds(buf):
    """Frame bytes -> (245, 2, 128, 3) uint8 with values 0..63."""
    bits = np.unpackbits(np.frombuffer(buf, np.uint8)).reshape(ROWS, PLANES, SEG, 3, 4)
    w = (1 << np.arange(5, -1, -1)).reshape(1, 6, 1, 1, 1)
    seg = (bits * w).sum(1)  # (245,64,3,4)
    A = np.concatenate([seg[..., 1], seg[..., 0]], 1)
    B = np.concatenate([seg[..., 2], seg[..., 3]], 1)
    return np.stack([B, A], 1).astype(np.uint8)


def encode_image(img, mirror, rotate, gamma, brightness):
    x, y = sample_coords(img.shape[0], mirror, rotate)
    v = bilinear(img, x, y) / 255.0
    v = np.clip((v ** gamma) * brightness, 0, 1)
    return np.round(v * 63).astype(np.uint8)


def square_array(img, fit):
    img = ImageOps.exif_transpose(img).convert("RGBA")
    bg = Image.new("RGBA", img.size, (0, 0, 0, 255))
    img = Image.alpha_composite(bg, img).convert("RGB")  # transparent -> black (LED off)
    if fit == "contain":
        img = ImageOps.pad(img, (SIZE, SIZE), color=(0, 0, 0))
    else:
        img = ImageOps.fit(img, (SIZE, SIZE))
    return np.asarray(img)


def video_frames(path, fit, fps):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        sys.exit("ffmpeg not found in PATH (required for video input)")
    if fit == "contain":
        vf = (f"scale={SIZE}:{SIZE}:force_original_aspect_ratio=decrease,"
              f"pad={SIZE}:{SIZE}:(ow-iw)/2:(oh-ih)/2:black")
    else:
        vf = f"scale={SIZE}:{SIZE}:force_original_aspect_ratio=increase,crop={SIZE}:{SIZE}"
    if fps:
        vf = f"fps={fps}," + vf
    proc = subprocess.Popen([ffmpeg, "-v", "error", "-i", path, "-vf", vf, "-an",
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    n = SIZE * SIZE * 3
    while True:
        buf = proc.stdout.read(n)
        if len(buf) < n:
            break
        yield np.frombuffer(buf, np.uint8).reshape(SIZE, SIZE, 3)
    proc.wait()


def input_frames(path, fit, fps, still_frames):
    """Yield 1024x1024 RGB frames from a video, animated GIF/WebP, image folder or still image."""
    if os.path.isdir(path):
        files = sorted(f for f in os.listdir(path) if os.path.splitext(f)[1].lower() in IMAGE_EXTS)
        if not files:
            sys.exit(f"no images found in {path}")
        for f in files:
            yield square_array(Image.open(os.path.join(path, f)), fit)
        return
    ext = os.path.splitext(path)[1].lower()
    if ext in VIDEO_EXTS:
        yield from video_frames(path, fit, fps)
        return
    img = Image.open(path)
    if getattr(img, "n_frames", 1) > 1:
        for fr in ImageSequence.Iterator(img):
            yield square_array(fr.copy(), fit)
        return
    arr = square_array(img, fit)
    for _ in range(still_frames):
        yield arr


def cmd_encode(args):
    n = 0
    last = None
    with open(args.output, "wb") as f:
        f.write(HEADER)
        for img in input_frames(args.input, args.fit, args.fps, args.frames):
            last = leds_to_frame(encode_image(img, args.mirror, args.rotate, args.gamma, args.brightness))
            f.write(last)
            n += 1
            if args.max_frames and n >= args.max_frames:
                break
            if n % 25 == 0:
                print(f"  {n} frames...", flush=True)
        if n == 0:
            sys.exit("no frames read from input")
        # stock files always have 12*k + 11 frames; pad by repeating the last one
        while not args.no_pad and n % 12 != 11:
            f.write(last)
            n += 1
        f.write(trailer(n))
    print(f"ok: {args.output} ({n} frames, {4 + n * FRAME_BYTES + 8} bytes)")


def render_preview(leds, size=512):
    """Render LED data as seen from the front of the fan."""
    yy, xx = np.mgrid[0:size, 0:size]
    x = -(xx - size / 2 + 0.5) / (size / 2); y = (yy - size / 2 + 0.5) / (size / 2)
    r = np.sqrt(x * x + y * y); th = np.arctan2(y, x) % (2 * np.pi)
    i = (LEDS - 1 - (r * LEDS).astype(int))
    out = np.zeros((size, size, 3), np.uint8)
    for arm, off in [(0, 0.0), (1, np.pi)]:
        a = (((th - off) % (2 * np.pi)) / np.pi * ROWS).astype(int)
        m = (r < 1) & (a < ROWS)
        out[m] = leds[a[m], arm, i[m]].astype(np.uint16) * 255 // 63
    return Image.fromarray(out)


def cmd_decode(args):
    with open(args.input, "rb") as f:
        data = f.read()
    n = (len(data) - 12) // FRAME_BYTES
    tail = data[-8:]
    print(f"header={data[:4].hex()} frames={n} trailer={tail.hex()} trailer_ok={tail == trailer(n)}")

    def frame(k):
        off = 4 + k * FRAME_BYTES
        return render_preview(frame_to_leds(data[off:off + FRAME_BYTES]), 320 if args.gif else 512)

    if args.gif:
        imgs = [frame(k) for k in range(0, n, args.step)]
        imgs[0].save(args.output, save_all=True, append_images=imgs[1:], loop=0,
                     duration=int(1000 * args.step / args.preview_fps))
        print(f"animated preview ({len(imgs)} frames): {args.output}")
    else:
        frame(args.frame).save(args.output)
        print(f"preview of frame {args.frame}: {args.output}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("encode", help="image, GIF, video or image folder -> fan .mp4 file")
    e.add_argument("input", help="image, animated GIF/WebP, video (mp4, mov...) or folder of numbered images")
    e.add_argument("output")
    e.add_argument("--frames", type=int, default=119, help="number of frames for a still image (default 119)")
    e.add_argument("--fps", type=float, default=None, help="resample the video to this frame rate")
    e.add_argument("--max-frames", type=int, default=0, help="limit the number of frames read")
    e.add_argument("--no-pad", action="store_true",
                   help="do not pad to 12*k + 11 frames (the stock file convention)")
    e.add_argument("--fit", choices=["contain", "cover"], default="contain",
                   help="contain = whole image with black borders; cover = crop to fill")
    e.add_argument("--mirror", action="store_true",
                   help="mirror horizontally (the default orientation is already correct on the device)")
    e.add_argument("--rotate", type=float, default=0.0, help="rotation in degrees")
    e.add_argument("--gamma", type=float, default=1.0, help="gamma correction (e.g. 2.2 darkens midtones)")
    e.add_argument("--brightness", type=float, default=1.0, help="brightness multiplier")
    e.set_defaults(func=cmd_encode)
    d = sub.add_parser("decode", help="render a PNG/GIF preview of a fan file")
    d.add_argument("input"); d.add_argument("output")
    d.add_argument("--frame", type=int, default=0, help="frame to render (PNG mode)")
    d.add_argument("--gif", action="store_true", help="render an animated GIF of all frames")
    d.add_argument("--step", type=int, default=2, help="GIF mode: use 1 of every N frames (default 2)")
    d.add_argument("--preview-fps", type=float, default=24.0, help="GIF mode: playback speed")
    d.set_defaults(func=cmd_decode)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
