# Opening Starlab from another computer

Starlab runs on one Linux computer (the *Starlab computer*, often a mini PC or Raspberry Pi next to the radios), and
you use it from a web browser on any other computer, tablet or phone. This guide covers how to reach it, how to sign
in, and how to log in to the Starlab computer with SSH for the few things that need a terminal.

## Contents

- [The short version](#the-short-version)
- [Tailscale (recommended)](#tailscale-recommended)
- [On your home network](#on-your-home-network)
- [Signing in](#signing-in)
- [Logging in to the Starlab computer with SSH](#logging-in-to-the-starlab-computer-with-ssh)
- [Who Starlab answers](#who-starlab-answers)
- [Changing how Starlab is reached](#changing-how-starlab-is-reached)
- [Last resort: an SSH tunnel](#last-resort-an-ssh-tunnel)
- [Problems](#problems)

## The short version

1. Run the installer on the Starlab computer and accept the defaults.
2. At the end it prints a list of **sign-in links**, one for each way the computer can be reached:

   ```
   ==> Done. Open Starlab in a browser with one of these sign-in links:
     Tailscale, HTTPS (recommended)  https://starlab.tail1234.ts.net/rf/login/…
     home network                    http://starlab.local:8751/rf/login/…
     home network                    http://192.168.1.40:8751/rf/login/…
     Tailscale                       http://starlab:8751/rf/login/…
   ```

3. On your laptop, open the link that matches how you reach the Starlab computer. The setup wizard opens.

The links work for 24 hours, until you finish setup. After that, a new link is good for 15 minutes, and once a
browser has signed in it stays signed in for 30 days.

## Tailscale (recommended)

[Tailscale](https://tailscale.com) is a free (for personal use) private network for your own devices. With it:

- you open Starlab at a fixed **HTTPS** address, such as `https://starlab.tail1234.ts.net`, from home or anywhere;
- you don't change anything on your router, and Starlab is never exposed to the internet;
- the browser's location button in the setup wizard works (it needs HTTPS).

### Set it up

1. **On the Starlab computer:**

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   ```

   `tailscale up` prints a link. Open it on any device and sign in with Google, Microsoft, GitHub or Apple to
   add the computer to your tailnet.
2. **On your laptop and phone:** install Tailscale from [tailscale.com/download](https://tailscale.com/download)
   and sign in with the same account.
3. **Turn on HTTPS for your tailnet (once):** open the Tailscale admin console at
   [login.tailscale.com/admin/dns](https://login.tailscale.com/admin/dns), and make sure **MagicDNS** is on.
   Then, under **HTTPS Certificates**, press **Enable HTTPS**.
4. **Run Starlab's installer** (or run it again if Starlab is already installed):

   ```bash
   sudo ./install.sh
   ```

   It finds Tailscale and asks *"Publish Starlab at https://… for your Tailscale devices?"*. Answer yes. Behind the
   scenes it runs `tailscale serve --bg --https=443 http://127.0.0.1:8751`, which gives Starlab a real certificate
   for that name. Only devices in your tailnet can open it.

The first time you open the HTTPS address it can take up to a minute while Tailscale gets the certificate.

To stop publishing it: `sudo tailscale serve --https=443 off`. Uninstalling Starlab does this for you.

## On your home network

With the default installation, Starlab also answers other computers on the same home or office network, at:

- `http://<computer name>.local:8751`, if the Starlab computer runs Avahi (Ubuntu, Debian and Raspberry Pi OS desktop
  do by default; on a minimal install, run `sudo apt install avahi-daemon`). Macs, iPhones and Windows 10/11
  understand `.local` names.
- `http://<its IP address>:8751`, which always works. Find the address with `hostname -I` on the Starlab
  computer, or in your router's list of devices.

This is plain HTTP: someone already on your network could read the traffic. That's usually fine at home. For
encryption, use the Tailscale HTTPS address, even at home.

## Signing in

Starlab has no passwords. You sign in by opening a signed link, made on the Starlab computer:

```bash
starlab web-link
```

It prints one link per address, as in [the short version](#the-short-version). Open the one for the way you reach
the computer. It doesn't matter which you use: they all sign in the same way.

- Until you finish the setup wizard, links last **24 hours**. Finishing setup retires them.
- After setup, links last **15 minutes**.
- Once you've opened a link, that browser stays signed in for **30 days**.
- `starlab web-link --first` prints only the best link, for copying into a message.
- If the Slack integration is set up, you can also ask it for a link.

`starlab web-link` runs on the Starlab computer itself. If you're not sitting at it, log in with SSH first (next
section), or use the [Slack integration](slack-integration.md).

## Logging in to the Starlab computer with SSH

SSH gives you a terminal on the Starlab computer from your laptop. You need it for `starlab web-link`, upgrades and
troubleshooting.

### 1. Turn on SSH on the Starlab computer (once)

Many desktop installs don't have the SSH server. Install and start it:

```bash
sudo apt install openssh-server
sudo systemctl enable --now ssh
```

Raspberry Pi OS: you can instead turn on SSH in Raspberry Pi Imager's settings when you write the card, or with
`sudo raspi-config` → *Interface Options* → *SSH*.

Note the account name you log in with on that computer (`whoami`) and its address (see
[On your home network](#on-your-home-network), or `tailscale ip -4` for its Tailscale address).

### 2. Log in from your laptop

macOS (Terminal), Linux (any terminal) and Windows 10/11 (PowerShell or Windows Terminal) all have `ssh` built in:

```bash
ssh you@starlab.local          # home network, by name
ssh you@192.168.1.40           # home network, by address
ssh you@starlab                # over Tailscale (MagicDNS name)
```

Replace `you` with the account name on the Starlab computer. The first time, ssh asks whether to trust the
computer's key: type `yes`. Then enter that account's password. You now have a terminal there. Run
`starlab web-link`, and type `exit` to leave.

With Tailscale you can skip passwords and keys entirely with
[Tailscale SSH](https://tailscale.com/kb/1193/tailscale-ssh): run `sudo tailscale set --ssh` on the Starlab
computer, then `ssh you@starlab` from any device in your tailnet.

### 3. Optional: log in without typing a password

Create a key on your laptop (once), and copy it to the Starlab computer.

macOS and Linux:

```bash
ssh-keygen -t ed25519                  # press Enter for each question
ssh-copy-id you@starlab.local          # asks for the password one last time
```

Windows (PowerShell):

```powershell
ssh-keygen -t ed25519
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh you@starlab.local "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

From then on, `ssh you@starlab.local` logs straight in.

### 4. Optional: a shortcut name

On macOS and Linux, add this to `~/.ssh/config` on your laptop (Windows: `C:\Users\<you>\.ssh\config`):

```
Host starlab
    HostName starlab.local
    User you
```

Then `ssh starlab` is enough. You can also run a single command without staying logged in:

```bash
ssh starlab starlab web-link
```

`starlab` commands that change things use `sudo`, so they may ask for your password on the Starlab computer.

## Who Starlab answers

Starlab only answers computers on **private networks**: the Starlab computer itself, home and office networks
(`10.x`, `172.16–31.x`, `192.168.x`, link-local), IPv6 private addresses, and Tailscale (`100.64.0.0/10`). Anyone
else gets a refusal, even if your router forwards the port by mistake.

This is `allowed_clients` in `starlab.toml`:

```toml
[broker]
host = "0.0.0.0"
allowed_clients = ["private"]                   # the default
# allowed_clients = ["192.168.1.0/24"]         # only this network (plus the Starlab computer itself)
# allowed_clients = ["any"]                     # no check: only behind a reverse proxy that controls access
```

Restart Starlab after changing it (`sudo systemctl restart starlab`). Requests through Tailscale HTTPS or a local
reverse proxy arrive from the Starlab computer itself and are always answered: the proxy decides who reaches it.

**Never forward Starlab's port on your router.** To reach it away from home, use Tailscale.

## Changing how Starlab is reached

Run the installer again with `--access`:

```bash
sudo ./install.sh --access network     # home network and Tailscale (the default)
sudo ./install.sh --access local       # only the Starlab computer itself (and Tailscale HTTPS, if set up)
```

This changes only the listen address in `starlab.toml` (keeping a backup copy next to it) and restarts Starlab.
`--access local` with Tailscale HTTPS is the tightest setup: nothing but your tailnet can reach Starlab, and only
over HTTPS.

## Last resort: an SSH tunnel

If Starlab answers only its own computer and you have no Tailscale, you can forward it over SSH. On your laptop,
leave this running:

```bash
ssh -N -L 8751:127.0.0.1:8751 you@starlab.local
```

It prints nothing while it works. Then make a link for that address on the Starlab computer, and open it on your
laptop:

```bash
starlab web-link --base-url http://127.0.0.1:8751
```

## Problems

| What you see | What to do |
|---|---|
| The page doesn't load at all | Check you used the right link for your network. On the Starlab computer, `systemctl status starlab` should say *active*. Try the IP address link instead of the `.local` one. |
| A `127.0.0.1` link doesn't open on your laptop | `127.0.0.1` always means "this computer". Use another link from `starlab web-link`, or set up a [tunnel](#last-resort-an-ssh-tunnel). |
| "Starlab only answers computers on this computer's private networks…" | You're connecting from outside your network, or through something that changes your address. Use Tailscale. |
| "This sign-in link has expired" | Run `starlab web-link` again (log in with SSH first), and open the new link within 15 minutes. |
| The Tailscale HTTPS address doesn't load | Is Tailscale connected on your laptop? Is HTTPS enabled for your tailnet? On the Starlab computer, `tailscale serve status` should show `proxy http://127.0.0.1:8751`. |
| `ssh: connect to host … port 22: Connection refused` | The SSH server isn't running: `sudo apt install openssh-server && sudo systemctl enable --now ssh`. |
| `Permission denied (publickey,password)` | Wrong account name or password. Use the account name on the Starlab computer, not your laptop's. |
| `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED` | Expected after reinstalling the Starlab computer's operating system. Otherwise, stop and find out why. To accept the new key: `ssh-keygen -R starlab.local`. |
