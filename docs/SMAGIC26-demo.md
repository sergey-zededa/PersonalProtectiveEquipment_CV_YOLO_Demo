# SMAGIC'26 live demo runbook

Slot: 10 minutes, after slide 20 ("Live Demo"). One node, one app, two connectivity states.

## Setup

| Item | Value |
|------|-------|
| Node | `smagic-ppe-demo`, OnLogic CL260 (Intel N150, 4 cores, 7.4 GB), EVE-OS 17.0.0-lts |
| Controller | https://zedcontrol.hummingbird.zededa.net, project `default-project` |
| App | `smagic26-ppe-detection` (image `ppe-stream_v6` = `sergeyzededa/zdemo:6`) |
| Instance | `smagic-ppe-demo-ppe`, 4 vCPU / 3 GB, port 8000 on the default local network instance |
| Camera | TANDBERG PrecisionHD USB on usbaddr 3:4; the app gets the whole USB controller `USB1` (PCI 00:14.0) |
| UI | `http://<node-ip>:8000` (home LAN: `http://192.168.0.108:8000`) |
| Cloud stream | `CAMERA_STREAM_URL`, default `https://sspm.freeddns.org/videos/playlist.m3u8` |

## Suggested slide 21 text (replaces the multi-vendor stack version)

**What you are about to see:** one rugged edge node, one AI app, the link cut mid-demo.

THE SETUP: Rugged edge server with EVE-OS. CCTV analytics with AI (container). USB camera on the node.

1. **Onboard a new node, zero touch.** Power on, it registers and pulls its configuration.
2. **Deploy an AI app from the marketplace.** One click, to one vessel or a whole fleet group.
3. **Online: analyse the cloud feed.** Recorded CCTV streamed from shore, inference on board.
4. **Cut the link.** The app switches to the local camera and keeps detecting.
5. **Reconnect.** The node reconciles and the app returns to the cloud feed on its own.

## Flow (10 min)

1. **Onboard (2 min).** ZEDEDA Cloud > Edge Nodes > `smagic-ppe-demo`: show serial number, model,
   EVE version, "Online", and the event log from registration. No local login at any point.
   A true live onboarding needs EVE reinstalled and the node deleted from the controller first,
   which takes about 15 minutes before the session. Only do that if there is time to rehearse it.
2. **Deploy (3 min).** Marketplace > `smagic26-ppe-detection` > Deploy > `smagic-ppe-demo`.
   Network: `defaultLocal-smagic-ppe-demo`. Adapters: assign `usbcam` to the USB controller `USB1`.
   Leave the custom config defaults. Talk over the boot (about 60 to 90 s from a cached image),
   then open `http://<node-ip>:8000`.
3. **Online (1 min).** Header shows "Uplink online", tag "Recorded · Cloud stream". Point at the
   hardhat and vest counts and the on-node FPS.
4. **Cut the link (2 min).** Pull the WAN cable of the travel router (node and laptop stay on its
   LAN, so the UI stays reachable). Within about 6 s: orange "Uplink lost" banner, tag "Live · USB camera", Source events
   shows "Cloud stream → USB camera". Walk into the camera view with a hardhat or vest. In ZEDEDA
   Cloud the node goes to "Suspect" after a few minutes while the app keeps running.
5. **Reconnect (2 min).** Plug the uplink back. Auto mode returns to the cloud stream after two
   good probes; the node reports "Online" again in ZEDEDA Cloud and its state reconciles.
   Optional: switch sources by hand from the side panel to show it is one app, three inputs.

## Before the session

- [ ] Image pushed: `docker buildx build --platform linux/amd64 -t sergeyzededa/zdemo:6 --push .`
- [ ] Expect about 1 fps on CPU-only inference. The N150 is held at a 6 W package limit by
      firmware (raising PL1 in MSR 0x610 has no effect; lowering it does), so the cores run at
      about 800 MHz under load. Turbo is enabled in BIOS.
- [ ] Delete any rehearsal instance a minute or two before the live deploy: redeploying while the
      old instance still holds the USB controller shows a brief "Error" state before booting.
- [ ] Cloud stream URL reachable **from the venue network** (see risks). Test with `curl -I <url>`.
- [ ] Full rehearsal on the venue network: `./deploy/zededa_deploy.py deploy`, open the UI, cut
      and restore the link, check the camera, then `./deploy/zededa_deploy.py undeploy`.
- [ ] Keep the image cached for the live deploy: device config item `timer.defer.content.delete`
      = `86400` (keeps deleted content for a day). Otherwise the live deploy re-downloads about
      600 MB over venue Wi-Fi.
- [ ] Travel router between the venue network and the booth: node and laptop on its LAN, venue
      on its WAN port. Pulling the WAN cable is the "cut the link" moment. Note the node's IP.
- [ ] Hardhat and hi-vis vest at the booth for the USB camera part.

## Risks to clear first

- **EVE version.** Base image pinned to `17.0.0-lts-kvm-amd64` on 2026-09-28 (it had been set to a
  PR build). Check nobody changes it before the event.
- **USB passthrough.** Per-port assignment (`PORTNO_*`) does not work for the camera: QEMU's
  emulated xHCI breaks UVC isochronous transfers. The app therefore takes the whole controller
  `USB1`, so every USB-A port (including any serial console adapter) belongs to the app while it
  runs. If the camera is re-plugged while the node is up with per-port assignment, EVE can also
  keep a stale bus address; controller passthrough avoids that too.
- **Cloud stream host.** `sspm.freeddns.org` points to a home connection, and port
  443 currently refuses connections. The online part of the demo depends on this server being up
  and reachable from the venue. A hosted location (for example, an object storage bucket serving
  the `sample-videos/` HLS files) would remove that dependency.
- **Hardware model.** The CL260 model's USB ports have an empty `parentassigngrp`, so the
  controller will not stop another app from taking the whole USB controller (`group4`) while a
  port is assigned. It does not affect this demo, which assigns only one port.

## Backup plan

- No uplink at the venue: start in `local` mode (on-node videos), then show the USB camera. The
  "cut the link" story still works from the ZEDEDA Cloud side if a phone hotspot is used as uplink.
- No controller access: screenshots or a screen recording of the rehearsal.
- App misbehaves: `./deploy/zededa_deploy.py status`, then restart the instance from ZEDEDA Cloud.
