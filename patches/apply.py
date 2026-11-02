#!/usr/bin/env python3
"""
Apply the Philips QM163E YouTube patches to a decoded APK tree.

Usage: apply.py <decoded_dir> <abi_split_lib_dir>

Patches, in order:
  1. manifest    - rename applicationId, make the APK self-contained (no ABI split)
  2. native      - compile the device identity into libcobalt.so
  3. gms         - stop the Play-services signature check from killing the process
  4. capability  - widen VideoCapabilities so the 60fps rungs are accepted
  5. branding    - invert the logo plate to black

Every patch asserts it actually applied. If one fails, nothing is written silently -
the script exits non-zero and tells you what to go look for.
"""
import os, re, sys, glob, shutil
from collections import deque

OLD_PKG = "com.google.android.youtube.tv"
NEW_PKG = "dev.chillbytes.youtubetv"
JAVA_PKG = "com.google.android.apps.youtube.tv"   # class names - must NOT be renamed

class PatchError(RuntimeError):
    pass

def smali_files(root):
    for d in sorted(glob.glob(os.path.join(root, "smali*"))):
        for dirpath, _, names in os.walk(d):
            for n in names:
                if n.endswith(".smali"):
                    yield os.path.join(dirpath, n)

# ---------------------------------------------------------------- 1. manifest
def patch_manifest(root):
    p = os.path.join(root, "AndroidManifest.xml")
    s = open(p).read()
    keep = s.count(JAVA_PKG)
    if OLD_PKG not in s:
        raise PatchError(f"applicationId {OLD_PKG} not found in manifest")
    s = s.replace(OLD_PKG, NEW_PKG)
    if s.count(JAVA_PKG) != keep:
        raise PatchError("renaming applicationId also mangled Java class names - aborting")
    s = s.replace('android:extractNativeLibs="false"', 'android:extractNativeLibs="true"')
    s = re.sub(r'\s+android:requiredSplitTypes="[^"]*"', '', s)
    open(p, "w").write(s)
    return f"applicationId -> {NEW_PKG}; libs extracted; ABI-split requirement removed"

# ------------------------------------------------------------------ 2. native
# Cobalt reads brand/model/chipset in native code, so this cannot be done in Java.
#
# It used to be enough to repoint the property NAMES at debug.* and set those over adb.
# That works but is volatile: a full boot clears debug.* (nothing persistent is writable
# by shell, and SELinux denies the app the property socket - see FINDINGS.md),
# and an empty identity makes YouTube refuse the device outright. So instead the value is
# baked in and the property lookup is bypassed altogether.
#
# SbSystemGetProperty is a switch; every case has the same three-instruction shape
#
#     ldr r0, [pc, #imm]        ; r0 = &"ro.product.model"
#     add r0, pc, r0
#     b   <shared tail>         ; mov r1, r5 ; bl __system_property_get ; if empty -> "unknown"
#
# and the tail is shared by every property, so its "unknown" fallback cannot be given
# three different values. The per-case `b` can, though. We
#
#   1. overwrite each property-name string in .rodata with the VALUE we want (the address
#      is unchanged, so the existing ldr/add still computes it),
#   2. write a small copy loop into inter-function alignment padding, and
#   3. retarget those three `b` instructions at it.
#
# The loop copies r0 -> the tail's buffer register and jumps to the tail's continuation,
# so the property is never consulted. It touches only r0-r2 (caller-saved and dead at the
# continuation) and uses no bl, so lr is preserved too.
IDENTITY = [(b"ro.product.brand",  b"Sony"),
            (b"ro.product.model",  b"BRAVIA_ATV3"),
            (b"ro.board.platform", b"mt5895")]

NOP = 0xE320F000

def _elf_sections(d):
    if d[:4] != b"\x7fELF" or d[4] != 1:
        raise PatchError("not a 32-bit ELF")
    u = lambda o, n: int.from_bytes(d[o:o + n], "little")
    shoff, shent, shnum, shstrndx = u(0x20, 4), u(0x2e, 2), u(0x30, 2), u(0x32, 2)
    raw = [[u(shoff + i * shent + k, 4) for k in (0, 4, 12, 16, 20)] for i in range(shnum)]
    stro = raw[shstrndx][3]
    out = {}
    for name_off, _t, addr, off, size in raw:
        nm = d[stro + name_off:d.index(b"\x00", stro + name_off)].decode()
        out[nm] = (addr, off, size)
    return out

def _arm_b(at, target, cond=0xE):
    delta = target - (at + 8)
    if delta % 4:
        raise PatchError("misaligned branch")
    off = delta >> 2
    if not -(1 << 23) <= off < (1 << 23):
        raise PatchError("branch out of range")
    return (cond << 28) | (0b101 << 25) | (off & 0xFFFFFF)

def patch_native(root):
    p = os.path.join(root, "lib", "armeabi-v7a", "libcobalt.so")
    if not os.path.exists(p):
        raise PatchError("libcobalt.so missing - was the ABI split copied in?")
    d = bytearray(open(p, "rb").read())
    S = _elf_sections(d)
    (TA, TO, TSZ) = S[".text"]
    w  = lambda o: int.from_bytes(d[o:o + 4], "little")
    put = lambda o, v: d.__setitem__(slice(o, o + 4), int(v).to_bytes(4, "little"))
    t2o = lambda a: TO + (a - TA)                      # .text vaddr -> file offset

    # -- locate each case: the ARM PIC load of the property name, and the `b` after it
    cases, tails = [], set()
    for name, value in IDENTITY:
        if len(value) > len(name):
            raise PatchError(f"{value!r} does not fit in {name!r}")
        if d.count(name + b"\x00") != 1:
            raise PatchError(f"expected exactly 1 occurrence of {name.decode()}")
        soff = d.index(name + b"\x00")
        svad = None
        for _a, _o, _s in S.values():
            if _a and _o <= soff < _o + _s:
                svad = _a + (soff - _o); break
        if svad is None:
            raise PatchError(f"{name.decode()} is not in an allocated section")

        hits = []
        for i in range(0, TSZ - 3, 4):
            v = w(TO + i)
            if (v & 0x0FFF0FF0) != 0x008F0000 or (v >> 28) != 0xE:
                continue                                # add rD, pc, rD
            rD = (v >> 12) & 0xF
            if (v & 0xF) != rD:
                continue
            add_at = TA + i
            for b in range(4, 80, 4):                   # the ldr rD,[pc,#imm] feeding it
                if i - b < 0:
                    break
                lv = w(TO + i - b)
                if (lv & 0x0FFF0000) == 0x059F0000 and ((lv >> 12) & 0xF) == rD:
                    lit = (TA + i - b) + 8 + (lv & 0xFFF)
                    if (add_at + 8 + w(t2o(lit))) & 0xFFFFFFFF == svad:
                        hits.append(add_at)
                    break
        if len(hits) != 1:
            raise PatchError(f"expected 1 ARM PIC reference to {name.decode()}, found {len(hits)}")
        br_at = hits[0] + 4
        br = w(t2o(br_at))
        if (br >> 24) != 0xEA:                          # unconditional b
            raise PatchError(f"{name.decode()}: expected `b` after the add, got {br:#010x}")
        off = br & 0xFFFFFF
        if off & 0x800000:
            off -= 1 << 24
        tails.add(br_at + 8 + off * 4)
        cases.append((name, value, soff, br_at))

    if len(tails) != 1:
        raise PatchError(f"the three cases no longer share one tail: {[hex(t) for t in tails]}")
    tail = tails.pop()

    # -- read the shared tail to learn the buffer register and the continuation address
    #      mov r1, rB / bl __system_property_get / ldrb r0,[rB] / cmp r0,#0 / bne CONT
    mov = w(t2o(tail))
    if (mov & 0xFFFFFFF0) != 0xE1A01000:
        raise PatchError(f"tail does not start with `mov r1, rB` ({mov:#010x})")
    rB = mov & 0xF
    if (w(t2o(tail + 4)) >> 24) != 0xEB:
        raise PatchError("tail does not call __system_property_get")
    if w(t2o(tail + 8)) != (0xE5D00000 | (rB << 16)):
        raise PatchError("tail does not test the buffer's first byte")
    if w(t2o(tail + 12)) != 0xE3500000:
        raise PatchError("tail does not `cmp r0, #0`")
    bne = w(t2o(tail + 16))
    if (bne >> 24) != 0x1A:
        raise PatchError("tail does not `bne` past the fallback")
    off = bne & 0xFFFFFF
    if off & 0x800000:
        off -= 1 << 24
    cont = tail + 16 + 8 + off * 4

    # -- find unreferenced alignment padding for the copy loop.
    # Padding is only safe if it is genuinely unreachable, so a candidate must (a) follow
    # an instruction that ends a function, (b) have nothing branching into it, and (c) have
    # no address inside it appear as a word anywhere in the file. (c) matters: .text here
    # also holds tables of code addresses, and one otherwise-fine NOP run turned out to be
    # pointed into by one.
    need = 6 * 4
    ENDS = (0xE12FFF1E, 0xE1A0F00E)                     # bx lr ; mov pc, lr
    runs, run = [], None
    for i in range(0, TSZ - 3, 4):
        if w(TO + i) == NOP:
            if run is None:
                run = i
        else:
            if run is not None and i - run >= need:
                runs.append(TA + run)
            run = None

    branch_targets = set()
    for i in range(0, TSZ - 3, 4):
        v = w(TO + i)
        if (v & 0x0E000000) != 0x0A000000:
            continue
        o = v & 0xFFFFFF
        if o & 0x800000:
            o -= 1 << 24
        branch_targets.add(TA + i + 8 + o * 4)

    cave, rejected = None, []
    for c in runs:
        prev = w(t2o(c) - 4)
        if not (prev in ENDS or (prev & 0xFFFF8000) == 0xE8BD8000 or (prev >> 24) == 0xEA):
            rejected.append((c, "not preceded by a function end")); continue
        if any(a in branch_targets for a in range(c, c + need, 4)):
            rejected.append((c, "branched into")); continue
        if any(bytes(d).find(a.to_bytes(4, "little")) != -1 for a in range(c, c + need, 4)):
            rejected.append((c, "address appears as a word in the file")); continue
        cave = c; break
    if cave is None:
        raise PatchError("no safe NOP padding for the copy loop; rejected: "
                         + ", ".join(f"{a:#x} ({why})" for a, why in rejected))

    #   mov  r2, rB          ; walking destination, leaves rB intact
    # L: ldrb r1, [r0], #1
    #   strb r1, [r2], #1
    #   cmp  r1, #0
    #   bne  L
    #   b    cont
    body = [0xE1A02000 | rB, 0xE4D01001, 0xE4C21001, 0xE3510000,
            _arm_b(cave + 16, cave + 4, cond=0x1), _arm_b(cave + 20, cont)]
    for k, insn in enumerate(body):
        put(t2o(cave) + k * 4, insn)

    # -- bake the values in and send each case to the cave
    done = []
    for name, value, soff, br_at in cases:
        d[soff:soff + len(name) + 1] = value + b"\x00" * (len(name) + 1 - len(value))
        put(t2o(br_at), _arm_b(br_at, cave))
        done.append(f"{name.decode()}={value.decode()}")
    open(p, "wb").write(bytes(d))
    return (f"hardcoded {', '.join(done)}; copy loop @ {cave:#x}, "
            f"buffer r{rB}, continuation {cont:#x}")

# --------------------------------------------------------------------- 3. gms
# A re-signed package makes Play services throw
#   SecurityException: PackageVerificationRslt: not allowed
# from a background handler, which kills the process. The throwing frame lives in
# a tiny obfuscated method whose NAME changes every release, so find it by shape:
#   void a(SomeConnectionResult p1) {
#       if (p1.b())      { this.a.p(null, this.a.v()); }   // <- throws
#       else             { ... .g(p1); }                   // graceful failure path
#   }
# Forcing the isSuccess() result to 0 sends it down its own failure branch.
def patch_gms(root):
    for path in smali_files(root):
        txt = open(path).read()
        if "Ljava/util/Set;)V" not in txt or "->v()Ljava/util/Set;" not in txt:
            continue
        lines = txt.split("\n")
        for i, l in enumerate(lines):
            if not (l.startswith(".method") and re.search(r"\ba\(L[^;]+;\)V", l)):
                continue
            end = next(j for j in range(i, len(lines)) if lines[j].strip() == ".end method")
            body = lines[i:end]
            joined = "\n".join(body)
            if "->v()Ljava/util/Set;" not in joined or "Ljava/util/Set;)V" not in joined:
                continue
            for k, bl in enumerate(body):
                if re.search(r"invoke-virtual \{p1\}, L[^;]+;->b\(\)Z", bl):
                    mr = next((m for m in range(k, min(k + 12, len(body)))
                               if body[m].strip() == "move-result v0"), None)
                    if mr is None:
                        continue
                    lines.insert(i + mr + 1, "    const/4 v0, 0x0")
                    open(path, "w").write("\n".join(lines))
                    return f"{os.path.basename(path)} {l.strip().split()[-1]} -> forced failure branch"
    raise PatchError(
        "GMS signature-check site not found.\n"
        "  Re-locate it: install unpatched, launch, then\n"
        "    adb logcat | grep -A12 'FATAL EXCEPTION'\n"
        "  Look for 'PackageVerificationRslt: not allowed'. The 3rd-from-top app frame\n"
        "  (e.g. 'at bwe.a(PG:14)') names the class+method to patch.")

# -------------------------------------------------------------- 4. capability
# Widen the framework's VideoCapabilities so the client accepts 1440p60/2160p60
# once the server offers them. CodecCapabilityInfo is JNI-bound, so NOT obfuscated.
CAP_CODE = '''    iget-object v0, p0, %(C)s->videoCapabilities:%(V)s
    if-eqz v0, :ytp_done
    const-class v1, %(V)s
    const-string v2, "mFrameRateRange"
    invoke-virtual {v1, v2}, Ljava/lang/Class;->getDeclaredField(Ljava/lang/String;)Ljava/lang/reflect/Field;
    move-result-object v2
    const/4 v3, 0x1
    invoke-virtual {v2, v3}, Ljava/lang/reflect/Field;->setAccessible(Z)V
    const/4 v3, 0x1
    invoke-static {v3}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;
    move-result-object v3
    const/16 v4, 0x3c
    invoke-static {v4}, Ljava/lang/Integer;->valueOf(I)Ljava/lang/Integer;
    move-result-object v4
    invoke-static {v3, v4}, Landroid/util/Range;->create(Ljava/lang/Comparable;Ljava/lang/Comparable;)Landroid/util/Range;
    move-result-object v3
    invoke-virtual {v2, v0, v3}, Ljava/lang/reflect/Field;->set(Ljava/lang/Object;Ljava/lang/Object;)V
    const-string v2, "mBlocksPerSecondRange"
    invoke-virtual {v1, v2}, Ljava/lang/Class;->getDeclaredField(Ljava/lang/String;)Ljava/lang/reflect/Field;
    move-result-object v2
    const/4 v3, 0x1
    invoke-virtual {v2, v3}, Ljava/lang/reflect/Field;->setAccessible(Z)V
    const-wide/16 v4, 0x1
    invoke-static {v4, v5}, Ljava/lang/Long;->valueOf(J)Ljava/lang/Long;
    move-result-object v3
    const-wide/32 v4, 0x1e8480
    invoke-static {v4, v5}, Ljava/lang/Long;->valueOf(J)Ljava/lang/Long;
    move-result-object v4
    invoke-static {v3, v4}, Landroid/util/Range;->create(Ljava/lang/Comparable;Ljava/lang/Comparable;)Landroid/util/Range;
    move-result-object v3
    invoke-virtual {v2, v0, v3}, Ljava/lang/reflect/Field;->set(Ljava/lang/Object;Ljava/lang/Object;)V
    :ytp_done'''

def patch_capability(root):
    hits = [f for f in smali_files(root)
            if f.endswith("dev/cobalt/media/MediaCodecUtil$CodecCapabilityInfo.smali")]
    if len(hits) != 1:
        raise PatchError(f"expected 1 CodecCapabilityInfo.smali, found {len(hits)}")
    p = hits[0]
    L = open(p).read().split("\n")
    C = "Ldev/cobalt/media/MediaCodecUtil$CodecCapabilityInfo;"
    V = "Landroid/media/MediaCodecInfo$VideoCapabilities;"
    start = next((i for i, l in enumerate(L) if l.startswith(
        ".method public constructor <init>(Landroid/media/MediaCodecInfo;Ljava/lang/String;)V")), None)
    if start is None:
        raise PatchError("CodecCapabilityInfo constructor signature changed")
    end = next(i for i in range(start, len(L)) if L[i].strip() == ".end method")
    loc = next(i for i in range(start, end) if L[i].strip().startswith(".locals"))
    have = int(L[loc].strip().split()[1])
    L[loc] = "    .locals %d" % max(have, 8)
    ret = max(i for i in range(start, end) if L[i].strip() == "return-void")
    L[ret:ret] = (CAP_CODE % {"C": C, "V": V}).split("\n")
    open(p, "w").write("\n".join(L))
    return "mFrameRateRange->[1,60], mBlocksPerSecondRange->[1,2000000]"

# ---------------------------------------------------------------- 5. branding
# Invert the logo plate to black. The red mark and the white play triangle inside
# it must survive, and the letter counters (holes in o/u/b/e) must go black - so
# protect the red region (plus anything it encloses) and recolour everything else.
def _is_red(px):
    r, g, b, a = px
    return a > 40 and r > 140 and r - max(g, b) > 60

def _invert(path, plate=(15, 15, 15)):
    from PIL import Image
    im = Image.open(path).convert("RGBA"); w, h = im.size; px = im.load()
    red = [[_is_red(px[x, y]) for x in range(w)] for y in range(h)]
    outside = [[False] * w for _ in range(h)]
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if not red[y][x] and not outside[y][x]:
                outside[y][x] = True; dq.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if not red[y][x] and not outside[y][x]:
                outside[y][x] = True; dq.append((x, y))
    while dq:
        x, y = dq.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not red[ny][nx] and not outside[ny][nx]:
                outside[ny][nx] = True; dq.append((nx, ny))
    for y in range(h):
        for x in range(w):
            if red[y][x] or not outside[y][x]:
                continue                                   # red mark + enclosed triangle
            r, g, b, a = px[x, y]
            if max(r, g, b) - min(r, g, b) >= 40:
                continue                                   # other colours
            v = (r + g + b) // 3
            if v < 200:                                    # wordmark -> white
                t = min(1.0, max(0.0, (200 - v) / 160.0)); s = int(round(255 * t))
                px[x, y] = (s, s, s, a)
            else:                                          # plate + counters -> black
                px[x, y] = (*plate, a)
    im.save(path)

def patch_branding(root):
    targets = (glob.glob(os.path.join(root, "res", "mipmap-*", "ic_launcher.png"))
               + glob.glob(os.path.join(root, "res", "drawable", "app_banner.png"))
               + glob.glob(os.path.join(root, "res", "drawable", "tv_channel_logo.png")))
    if not targets:
        raise PatchError("no branding assets found (res/ layout changed?)")
    for t in sorted(targets):
        _invert(t)
    return f"{len(targets)} asset(s) inverted to a black plate"

# --------------------------------------------------------------------- driver
def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(2)
    root, libdir = sys.argv[1], sys.argv[2]
    dest = os.path.join(root, "lib", "armeabi-v7a")
    if os.path.isdir(libdir) and not os.path.isdir(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copytree(libdir, dest)
    failed = 0
    for name, fn in (("manifest",   patch_manifest),
                     ("native",     patch_native),
                     ("gms",        patch_gms),
                     ("capability", patch_capability),
                     ("branding",   patch_branding)):
        try:
            print(f"  [ok]   {name:11s} {fn(root)}")
        except Exception as e:
            print(f"  [FAIL] {name:11s} {e}"); failed += 1
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
