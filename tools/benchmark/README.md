# Media server benchmark

Compares Noxa, Jellyfin and Plex on the same machine, with the same test library and the same limits. The whole run is automatic: it installs what is missing, builds the test library, starts each server in turn, sets it up through its own API, measures it, and removes it.

## Run it on Proxmox VE (simplest)

On the Proxmox host, as root, create a ready-to-use VM. It's a Debian 12 VM with 4 cores, 8 GB and 24 GB, and the script installs Docker, the three servers and the test library in it, which takes 15 to 25 minutes:

```bash
curl -fsSL https://raw.githubusercontent.com/xenix13/Noxa/test/tools/benchmark/proxmox-vm.sh -o noxa-bench-vm.sh
bash noxa-bench-vm.sh
```

Then run the benchmark. It takes about an hour, keeps going if the window is closed, and copies the results back to `/root/noxa-bench/results/` on the host:

```bash
bash noxa-bench-vm.sh run      # or "quick" for a 10-minute check
bash noxa-bench-vm.sh delete   # afterwards: removes the VM
```

The VM gets its address from DHCP on `vmbr0`. `VMID`, `STORAGE`, `BRIDGE`, `CORES`, `MEMORY` and `DISK` can be set in front of the command, for example `BRIDGE=vmbr1 bash noxa-bench-vm.sh`. If no storage accepts cloud-init *snippets* yet, the script allows them on `local`.

## Run it on any Debian or Ubuntu machine

On a **fresh Debian 12 or Ubuntu 24.04 VM**, as root:

```bash
curl -fsSL https://raw.githubusercontent.com/xenix13/Noxa/main/tools/benchmark/run.sh | bash
```

The run takes about an hour. At the end it prints a summary and tells you where the files are (`/opt/noxa-bench/results/<date>/`).

Until the kit reaches `main`, add `NOXA_BENCH_BRANCH=test` in front of `bash`: `curl -fsSL https://raw.githubusercontent.com/xenix13/Noxa/test/tools/benchmark/run.sh | NOXA_BENCH_BRANCH=test bash`.

To check that everything works first (about 10 minutes, the numbers mean nothing):

```bash
curl -fsSL https://raw.githubusercontent.com/xenix13/Noxa/main/tools/benchmark/run.sh | bash -s -- --quick
```

**The VM:**
- 4 cores or more: each server gets 2, the rest is for the test itself.
- 8 GB of RAM and 20 GB of disk.
- Nothing else running on ports 8420, 8096 and 32400.
- A VM rather than an LXC container, because Docker inside LXC needs extra settings.

Afterwards, `rm -rf /opt/noxa-bench && docker image prune -a` frees the space.

## What is measured

Each server runs alone, in a container limited with `--cpus` and `--memory` (2 CPUs and 4 GB by default), with the library mounted read-only.

| Step | Measure |
|---|---|
| Image | Size of the Docker image |
| Start and setup | Time until the server answers and is set up |
| Scan | Time until every title is listed; CPU and memory peak |
| Background work | Time until the CPU averages under 5 % over 60 s after the scan (at most 15 minutes) |
| Idle | Average CPU and memory over 5 minutes |
| Response times | Median and 95th percentile of 50 requests: a library page of 60 movies, one movie's details |
| Direct play | 3 clients reading 3 different 8 Mbit/s files at playback speed for 90 s: CPU and memory |
| Conversion | One HEVC 10-bit 1080p file converted to H.264 720p in software: speed (× real time), CPU and memory |

CPU and memory come from the Docker stats API once a second. CPU is in % of one core, so 200 % means two cores fully used. Memory excludes the page cache, like `docker stats`.

## How each server is set up

The goal is the defaults, with the same choices for everyone where they differ:

- **Online metadata off** everywhere. Plex uses its *Personal Media* agent, Jellyfin has no metadata or image fetchers, and Noxa has no TMDB key. Otherwise the scan would mostly measure the speed of the internet databases.
- **Preview thumbnails off** everywhere:
  - Noxa: the *trickplay* setting is off.
  - Jellyfin: trickplay and chapter images are off (its default).
  - Plex: "generate video preview thumbnails" is set to never (its default only runs at night).
  - Plex's intro, credits and loudness analysis are also set to never.
- **Everything else** is each server's default.
- **Accounts:** Plex is not claimed, so it needs no account; its API is reached from the same machine. Jellyfin and Noxa get a `bench` admin account.

The test library is generated with ffmpeg (a test pattern, not real films):
- **30 movies**, cycling three sources of 90 s:
  - 1080p H.264 8 Mbit/s with AAC;
  - 1080p HEVC 10-bit with AC3 5.1;
  - 720p H.264 with two audio tracks and subtitles.
- **3 shows** of 2 seasons × 8 episodes, 30 s each.

Files are real copies: Plex counts hard links to one file as a single title.

## Limits

- **Generated video is not real content.** CPU numbers for conversion depend on the content, so compare servers with each other, not with other benchmarks.
- **Plex's conversion** is driven through its web transcoder API with a generic client profile. A real Plex app may ask for slightly different settings.
- **Background work after the scan** differs a lot between servers (Plex analyses every file, Noxa prepares subtitles). Its duration is reported, but it isn't counted in the idle numbers.
- **One machine, one run.** Run it again if a number looks odd, and compare the raw samples (`samples-<server>.csv`).

## Options

`python3 bench.py --help` lists them:
- `--servers noxa,jellyfin` to run only some servers;
- `--cpus 4 --memory 8g` to change the limits;
- `--idle`, `--play` and `--settle` to change the durations.

Versions are pinned at the top of `bench.py` (`IMAGES`).
