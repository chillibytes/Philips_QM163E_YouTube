# Findings

How the 720p30 cap on the Philips 49PUS7101 was traced, what was ruled out, and how each patch works. Investigated and solved on 2026-09-06, and made to survive a reboot on 2026-09-07.

## The device

| | |
|---|---|
| Model | Philips 49PUS7101/12, `QM163E`, board `QM16XE_U` |
| SoC | MediaTek MT5890 |
| OS | Android 8.0.0, API 26, build `OPR5.170623.014` |
| Firmware | `QM163E.0.26.70.1` (2020-05-29, final for this chassis) |
| RAM | 1.37 GB, `ro.config.low_mem=true` |
| Panel | 4K, `sys.display-size=3840x2160` |
| Android display plane | **1920x1080 @ 50 Hz**, single mode |
| Root | none; exploits exist only for the Android 5 and 6 firmwares |

## Root cause

YouTube was **not** limited by the TV's hardware. It was limited by a **server-side device profile**, keyed on the device identity the app reports. The entry for `PHILIPS` + `QM163E` serves a 30fps-only format ladder.

Proven by changing **one character** of the reported model:

| Reported identity | 4K60 video result |
|---|---|
| `PHILIPS` / `QM163E` (stock) | `Max 720p`, itag 247 @ 30fps |
| `PHILIPS` / `QM163X` | ramps to `3840x2160` |
| `Sony` / `BRAVIA_ATV3` / `mt5895` | `Max 2160p60` |
| `PHILIPS` / `QM16XE_U` (real codename) | `Max 1080p60`, also a known, capped device |
| `PHILIPS` / `OPR5.170623.014` | `Max 1080p60` |

So an unknown identity is not enough: the string has to land on a permissive profile. Model **year** (parsed from the last two digits of `ro.oem.key1`, `ATV00004916` → 2016) turned out to be irrelevant. Brand, model and chipset together are what matter.

Corroborating evidence: on a 60fps video whose *web* ladder has no 30fps rung above 480p, the TV client was served itag **247** (720p30), a rendition the web client is never offered. The server was building a 30fps ladder for this device on purpose.

The fix has two halves, and neither works alone:

1. **Report another device**, so the server sends the 60fps ladder at all (patch 2, `native`).
2. **Widen the client's codec limits**, so it accepts the top rungs once they are offered (patch 4, `capability`).

A third patch (3, `gms`) is only needed to make a re-signed build boot.

## Dead ends

All disproven. Don't redo them.

- **Codec XML limits.** `/system/etc/media_codecs.xml` declares VP9 with `frame-rate 1-30` and `blocks-per-second 1-1105920` (about 4K@34). It looks like a perfect explanation and is not the gate. The *same file* measures the decoder at `measured-frame-rate-3840x2160 range="66-66"`.
- **Hardware capability.** VLC plays a local 3840x2160@59.94 VP9 clip in real time (40.5 s wall clock for a 41.4 s clip, 5 late frames, all during startup). VLC selects the decoder by MIME and so ignores the declared limits. The silicon is fine.
- **Display refresh rate.** The panel reports 50 Hz, but `DisplayUtil` never exposes the refresh rate to native code, and there is no such string in `libcobalt.so`. The two classes that call `getRefreshRate()` are jank telemetry.
- **Bandwidth, buffer and memory.** 38 to 136 Mbps measured, 65 to 91 s of buffer, 0 dropped frames, 216 MB free. Auto quality still refused to climb.
- **Java capability patches.** All verified applied, and all ineffective on their own: forcing `areSizeAndRateSupported` to true in `findVideoDecoder`, and raising VP9 `CodecProfileLevel.level` (the decoder really declares **VP9 Level 5.0**, which is 4K@30). `findVideoDecoder` turned out never to be called at all: instrumentation logged zero invocations. Everything flows through `getAllCodecCapabilityInfos()` into native code.
- **Letting the app set `debug.*` properties itself.** Tested end to end on 2026-09-07. A `YtIdentity.apply()` call was injected at the top of `StarboardBridge.<clinit>`. That is the right seam: it is where `System.loadLibrary("cobalt")` runs, so it provably runs before native code reads anything. The call set the properties with `android.os.SystemProperties.set` through reflection. The seam works and the call fires. The write does not:

  ```
  avc: denied { write } for name="property_service" dev="tmpfs"
       scontext=u:r:untrusted_app:s0 tcontext=u:object_r:property_socket:s0
  ```

  SELinux refuses `untrusted_app` the property-service **socket**, so this is not about `debug_prop` in particular. No app on this TV can set any property, and `Runtime.exec("setprop")` hits the same denial. Only the `shell` domain (adb) can write `debug.*`, which is why the identity is now compiled into `libcobalt.so` instead.

## The patches

All applied by `patches/apply.py`, and each one checks that it applied.

**1. `manifest`** renames the applicationId to `dev.chillbytes.youtubetv`, so the build installs *next to* the untouched official app. A system package can't be replaced by an APK signed with another key. This is safe because Cobalt's JNI binds to the *Java* package `dev.cobalt.*`, which is independent of the applicationId, and every component name in the manifest is already fully qualified. It also sets `extractNativeLibs="true"` and drops `requiredSplitTypes`, so the build is a single self-contained APK.

**2. `native`** compiles the device identity into `libcobalt.so`. Cobalt reads the identity in native code, so this can't be done in Java. The property lookup is bypassed entirely, so the identity survives a reboot with nothing to re-apply.

`SbSystemGetProperty` is a switch. Every case has the same three-instruction shape, and they all funnel into one shared tail:

```
ldr r0, [pc, #imm]      ; r0 = &"ro.product.model"
add r0, pc, r0
b   <tail>              ; mov r1, r5 ; bl __system_property_get
                        ; if the result is empty -> write "unknown"
```

Because the tail and its `"unknown"` fallback are shared by *every* property, the tail can't be given three different values. The `b` in each case can, so the patch:

1. overwrites each property-name string in `.rodata` with the **value** to report. The address is unchanged, so the existing `ldr`/`add` still computes it;
2. writes a copy loop into inter-function alignment padding;
3. retargets those three `b` instructions at the loop.

```
ro.product.brand   -> "Sony"
ro.product.model   -> "BRAVIA_ATV3"
ro.board.platform  -> "mt5895"
```

```arm
    mov  r2, r5          @ walking destination; leaves the tail's buffer register intact
L:  ldrb r1, [r0], #1
    strb r1, [r2], #1
    cmp  r1, #0
    bne  L
    b    <tail continuation>
```

Only r0 to r2 are touched, which are caller-saved and dead at the continuation, and there is no `bl`, so `lr` is preserved too. Everything is located by shape and asserted: the string, its single ARM PIC reference, the `b` after it, that all three cases share one tail, and the tail's exact `mov r1,rB / bl / ldrb r0,[rB] / cmp r0,#0 / bne` encodings, which is where the buffer register and the continuation address come from.

The padding used for the loop must be truly unreachable, so a candidate is rejected unless it follows a function-ending instruction, has nothing branching into it, and has no address inside it appearing as a word anywhere in the file. That last check matters: `.text` here also holds tables of code addresses, and the first padding run that passed the other two checks was pointed into by one.

**3. `gms`** stops Play services from killing a re-signed build. Play services throws `SecurityException: PackageVerificationRslt: not allowed` from a background handler, which kills the process on launch. One instruction (`const/4 v0, 0x0`) sends the connect down its own graceful-failure branch. The class is obfuscated and **renamed every release**, so the script finds it by shape, not by name. If that ever fails, it prints how to find it again from logcat.

> Hand-written smali `try/catch` blocks would **not** assemble under apktool 3.0.3 when inserted into an existing method. They do assemble in a new standalone class. Single-instruction edits work fine, so prefer those.

**4. `capability`** sets `mFrameRateRange` to `[1,60]` and `mBlocksPerSecondRange` to `[1, 2000000]` by reflection in `MediaCodecUtil$CodecCapabilityInfo.<init>` (4K60 needs 1,944,000 blocks per second). The class isn't obfuscated, because it's JNI-bound. Without this, the ladder tops out at 1080p60 even with the identity changed.

**5. `branding`** inverts the logo plate to black, so you can tell the two apps apart in the launcher. It protects the red mark *and anything it encloses* (the white play triangle), so letter counters go black but the triangle stays white.

## Troubleshooting

### "YouTube is not supported on this device"

A full-screen block with a QR code to `yt.be/qr/tv-devices`. This is what an **unusable device identity** looks like: YouTube refuses a device it doesn't recognise outright, rather than only capping it.

An earlier version kept the identity in `debug.*` properties, which a full boot clears. Cobalt's own fallback then reported brand and model as `"unknown"`, and `unknown`/`unknown` is refused. The identity is now compiled in, and a cold boot with every `debug.yt*` property unset was verified to load and play 2160p60.

If it ever comes back, check that the values in `.rodata` are intact (build with `--keep`):

```sh
strings build/decoded/lib/armeabi-v7a/libcobalt.so | grep -x 'BRAVIA_ATV3'
```

Then rule out a server-side change by launching the untouched official app:

```sh
adb shell monkey -p com.google.android.youtube.tv -c android.intent.category.LEANBACK_LAUNCHER 1
```

The official app reports the real `PHILIPS`/`QM163E`. If it loads normally, the problem is this build's identity. If it's blocked too, Google has dropped the device or this Cobalt version on its servers, which is a different problem.

### High load average is normal

`uptime` reports about 77 on an idle TV. Those are MediaTek TV driver kernel threads (`CbusThread`, `Demux`, `HDMI2PhyThd` and others) permanently in uninterruptible sleep, which Linux counts toward load. Check `top` instead: the CPU sits near 100% idle.

### adb says `unauthorized`

Run `adb kill-server && adb start-server && adb connect <tv-ip>:5555`. The key in `~/.android/adbkey` hasn't changed; the handshake just needs restarting.

## Diagnostics

Read the resolution the TV actually decodes without touching the UI:

```sh
adb logcat -c
# play something, then:
adb logcat -d | grep -E 'set picture info|Disp resolution change'
```

In `Disp resolution change (A) -> (B)`, A is the configured maximum and B the real output.

In the app: player, then the gear, then **Stats for nerds**. `Current / Optimal Res` and the itag in `Codecs` are the ground truth (`315` = 2160p60 VP9, `313` = 2160p30, `247` = 720p30).
