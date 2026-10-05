# Running the bot on your iMac

This sets the bot up so it runs all the time on your iMac and comes back by itself after a power cut.
It takes about 15 minutes, once.

How it fits together:

- The **dashboard** is a small web page that runs on the Mac at http://127.0.0.1:5050. Only this Mac can open it.
- The dashboard starts the **bot** and keeps it running. If the bot stops unexpectedly, the dashboard starts it again.
- A macOS **LaunchAgent** opens the dashboard every time you log in, so after a power cut:
  the iMac turns itself on, logs in, the dashboard opens, and the bot carries on where it stopped.
- If you turned the bot **off** in the dashboard, it stays off after a restart until you turn it on again.

## 1. iMac settings (power cuts)

1. **Start after a power failure.** Apple menu > System Settings > Energy (on older macOS: Energy Saver).
   Turn on **Start up automatically after a power failure**.
   If you don't see it, run this in Terminal: `sudo pmset autorestart 1`
2. **Don't sleep.** In the same Energy settings, turn on **Prevent automatic sleeping when the display is off**.
   The screen can still turn off; the iMac just must not go to sleep.
3. **Log in automatically.** System Settings > Users & Groups > **Automatically log in as** > choose your user.
   This is needed because the bot runs in your user account. (macOS hides this option when FileVault is on.
   In that case the iMac waits at the password screen after a power cut and the bot starts once you log in.)

## 2. Install the bot

Open **Terminal** (Applications > Utilities > Terminal) and run these lines one at a time.

Keep the bot in your home folder, not in Desktop, Documents or Downloads. macOS blocks background
programs from those folders unless you give extra permissions.

```bash
cd ~
git clone https://github.com/BlueFar/OpenSea-NFT-Trading-Bot.git nft-bot
cd nft-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
open -e .env
```

The last line opens `.env` in TextEdit. Replace `your_opensea_api_key_here` with your OpenSea API key,
then save and close. Keep this key private: don't paste it into chats or emails.

If `python3` asks to install the developer tools, click Install and run the line again afterwards.

## 3. Turn on auto-start

Still in Terminal, inside the `nft-bot` folder:

```bash
source .venv/bin/activate
python bot.py install-autostart
```

Then open http://127.0.0.1:5050 in Safari (add it to your Favourites) and switch the bot **On** on the Home page.

That's it. To check it works, restart the iMac: after logging in, the dashboard should open
at the same address and the bot should be running again within a few seconds.

## Everyday use

- **Home** shows whether the bot is running, what it checked today and why collections were rejected.
- **Candidates** are collections that passed every rule. Click one for the trade plan and its Info.md.
  Use **Sort by** and **Filters** (for example a minimum spread and your budget as the maximum buy price) to narrow the list.
  These only change what's shown, not the bot's rules, and the page remembers them.
- **Trading pace** also checks what buyers paid. At least 1 of last week's sales must be at about the floor
  price (90–115% of the floor at the time), so a collection whose sales are all people accepting the top offer
  doesn't pass. Each candidate shows a "What buyers paid" list. Change the number in Settings (0 turns it off).
- **Dollar values:** point at any coin amount (or tap it on a phone) to see it in US dollars now and, for candidates,
  when they were found. Prices come from OpenSea, with CoinGecko filling in coins OpenSea hasn't priced yet.
- **Near misses** are collections that missed one rule by a small margin. If one rule keeps showing up here, it may be too strict.
- **Check a collection** tests any OpenSea collection against your rules right now.
- **Settings** switches rules on or off, changes limits, picks chains and sets gas per chain.
  Changes are used from the bot's next check. They are saved in `config/overrides.yaml`.

In the first week the bot is still collecting floor prices, so the 7-day floor check can't pass yet.
The Home page shows the date candidates can start appearing.

## If something goes wrong

- **The page doesn't open:** in Terminal, inside `nft-bot`, run `source .venv/bin/activate` and then
  `python bot.py install-autostart` again.
- **Logs:** `bot.log` (the bot) and `logs/dashboard.log` (the dashboard), both in the `nft-bot` folder.
- **Turn auto-start off:** `python bot.py uninstall-autostart`
- **Internet down:** the bot pauses by itself and carries on when the connection is back. Nothing is marked as failed while offline.

## Updating

```bash
cd ~/nft-bot
git pull
source .venv/bin/activate
pip install -r requirements.txt
python bot.py install-autostart
```

The last line restarts the dashboard with the new version. Your settings, history and candidates are kept.
