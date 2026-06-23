# Deploying Bellwether on Oracle Cloud (Always Free, 24/7)

Goal: run the bot forever on a free Oracle "Always Free" VM, supervised by
`systemd` so it auto-restarts and survives reboots. ~20 minutes.

The bot only makes **outbound** connections (Kraken, Groq, news RSS, email/SMS),
so the VM needs no inbound ports except SSH — a small attack surface.

---

## 1. Create the VM (you do this in the Oracle console)

> Account creation needs a credit card for identity verification, but
> "Always Free" shapes are never charged. That part is yours to complete.

1. **Compute → Instances → Create instance.**
2. **Image:** Canonical **Ubuntu 22.04** (or 24.04).
3. **Shape:** *Change shape* → **Ampere (ARM)** → **VM.Standard.A1.Flex**, set
   **1 OCPU / 6 GB** (well within Always Free; the bot is tiny). Look for the
   green **"Always Free eligible"** tag.
   - If you get **"out of host capacity"** for A1 (common), either retry in a
     different Availability Domain, or pick the AMD **VM.Standard.E2.1.Micro**
     (also Always Free — 1/8 OCPU, 1 GB; still enough).
4. **SSH keys:** choose *Generate a key pair* and **download the private key**
   (or paste your existing public key). You'll need it to log in.
5. **Networking:** leave defaults (a public IP + a subnet that allows SSH).
6. **Create.** When it's "Running", copy the **public IP**.

---

## 2. Get the code onto the VM

From your Mac. **Never copy `.venv`** (it's the wrong CPU architecture — the
setup script rebuilds it) and **never copy `.env`** with real keys into a repo.

**Option A — scp (simplest, no GitHub):**
```bash
cd /Users/sharib/Desktop/Better
chmod 600 ~/path/to/your-oracle-key.pem
scp -i ~/path/to/your-oracle-key.pem -r \
    --exclude .venv --exclude bellwether-data --exclude .env \
    bellwether ubuntu@<PUBLIC_IP>:/home/ubuntu/bellwether
```
(`scp` has no `--exclude`; if yours errors, `rsync -av --exclude ...` instead, or
just delete `.venv` on the VM after copying.)

**Option B — GitHub:** push the repo (private), then on the VM
`git clone <repo> /home/ubuntu/bellwether`.

---

## 3. Provision + configure (on the VM)

```bash
ssh -i ~/path/to/your-oracle-key.pem ubuntu@<PUBLIC_IP>

cd /home/ubuntu/bellwether
bash deploy/setup_vm.sh          # installs python venv + deps, runs the tests

# Set the clock so the daily report fires at the right local hour:
sudo timedatectl set-timezone America/Toronto

# Configure
cp config.example.yaml config.yaml      # if not already present
cp .env.example .env
nano .env          # set GROQ_API_KEY now; add KRAKEN_API_KEY/SECRET for --live
chmod 600 .env     # lock down the secrets file
nano config.yaml   # see next section
```

### config.yaml for going live
```yaml
mode: kraken                 # real Kraken prices
risk:
  starting_bankroll: 200.0   # set to the REAL USD you funded Kraken with
  max_position_per_trade: 40.0
  max_daily_spend: 120.0
  max_open_positions: 5
notify:
  channel: email             # or sms
```
> On the first `--live` cycle the bot reads your real Kraken balance and
> re-baselines P&L to it, so `starting_bankroll` only needs to be roughly right.

---

## 4. Run it (paper-on-real-prices first, then live)

```bash
sudo cp deploy/bellwether.service /etc/systemd/system/bellwether.service
sudo systemctl daemon-reload
sudo systemctl enable --now bellwether
journalctl -u bellwether -f          # watch it trade live
```

The service ships **safe by default**: `mode: kraken` with **no `--live`**, so it
trades against real prices but **simulates fills** — zero money at risk. Let it
run for a day, confirm the cycles and the daily report look right, then flip to
real orders:

```bash
sudo nano /etc/systemd/system/bellwether.service
#   comment the plain ExecStart line,
#   uncomment the one ending in `--live run`
sudo systemctl daemon-reload && sudo systemctl restart bellwether
```

---

## 5. Push-to-deploy (optional, recommended)

Make the box update itself whenever you `git push`. A `systemd` timer checks the
GitHub remote every 2 minutes; on a new commit it fast-forwards, reinstalls deps
only if `requirements.txt` changed, and restarts the bot. It's **pull-based** —
no inbound ports, no secrets on GitHub — matching the outbound-only posture.

> Requires the code to be a **git clone** (Section 2, Option B), not an scp copy,
> and the repo to be reachable (public, or with credentials configured for the
> `ubuntu` user). With no upstream branch the updater just no-ops.

One-time install on the VM (after the bot service from Section 4 is running):
```bash
cd ~/bellwether && git pull          # get these deploy files onto the box once
sudo cp deploy/bellwether-update.service deploy/bellwether-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bellwether-update.timer

# Install the `bw` helper on your PATH (remove any old `alias bw=` first):
sudo ln -sf ~/bellwether/deploy/bw /usr/local/bin/bw
```

After this, the workflow is just: **push on your Mac → wait ≤2 min → it deploys.**
Watch it happen live:
```bash
bw watch        # tails the bot + auto-updater; you'll see the deploy land + new cycles
```
When a push lands you'll see `bellwether-update: new commit … — deploying`, the
service restart, then the bot's cycles running the new code. To force a check now
instead of waiting for the timer: `bw update`.

---

## Operating it

The `bw` helper (installed above) wraps everything; the raw commands are shown too.

| Task | Command |
|---|---|
| Watch deploys + trading live | `bw watch` (= `journalctl -u bellwether -u bellwether-update -f`) |
| Deploy latest now | `bw update` (= `sudo systemctl start bellwether-update.service`) |
| Status | `systemctl status bellwether` |
| Stop / start | `sudo systemctl stop bellwether` / `start` |
| Manual update (no auto-deploy) | `git pull` → `sudo systemctl restart bellwether` |
| Run the learning loop now | `bw reflect` |
| One-off report | `bw report` |

## Security checklist (real money is involved)
- [ ] Kraken API key has **Query Funds + Create/Modify Orders only** — **Withdraw OFF**.
- [ ] `.env` is `chmod 600` and never committed.
- [ ] Firewall to SSH only: `sudo ufw allow OpenSSH && sudo ufw enable`.
- [ ] Conservative risk caps in `config.yaml` until you trust it.
- [ ] Watched it in paper-on-real-prices for a day before `--live`.
