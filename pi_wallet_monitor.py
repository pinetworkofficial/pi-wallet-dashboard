import os
import json
import time
import smtplib
import requests
import pandas as pd
from datetime import datetime, timezone
from email.message import EmailMessage

HORIZON_URL = "https://api.mainnet.minepi.com"
STATE_FILE = "wallet_state.json"

WALLET_GROUPS = {
    "WC": "pi_wallets_wc.csv",
    "WCD": "pi_wallets_wcd.csv",
    "RP": "pi_wallets_rp.csv",
    "LP": "pi_wallets_lp.csv",
    "Pi done by me and others": "pi_wallets_donebymeothers.csv",
}

def parse_iso_timestamp(value):
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None

def find_time_conditions(predicate, creation_time=None, negated=False):
    unlock_times = []
    if not isinstance(predicate, dict):
        return unlock_times
    if "not" in predicate:
        unlock_times += find_time_conditions(predicate["not"], creation_time, not negated)
    for key in ("abs_before", "absBefore", "abs_before_epoch", "absBeforeEpoch"):
        if key in predicate:
            try:
                ts = int(predicate[key])
            except Exception:
                ts = parse_iso_timestamp(predicate[key])
            if ts is not None and negated:
                unlock_times.append(ts)
    for key in ("rel_before", "relBefore"):
        if key in predicate:
            try:
                seconds = int(predicate[key])
                if creation_time is not None and negated:
                    unlock_times.append(creation_time + seconds)
            except Exception:
                pass
    for key in ("and", "or"):
        children = predicate.get(key)
        if isinstance(children, list):
            for child in children:
                unlock_times += find_time_conditions(child, creation_time, negated)
    return unlock_times

def get_account(address):
    r = requests.get(f"{HORIZON_URL}/accounts/{address}", timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def get_claimable_balances(address):
    r = requests.get(
        f"{HORIZON_URL}/claimable_balances",
        params={"claimant": address, "limit": 200},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("_embedded", {}).get("records", [])

def get_lockups(address):
    now = int(datetime.now(timezone.utc).timestamp())
    lockups = []
    for cb in get_claimable_balances(address):
        if cb.get("asset", "native") != "native":
            continue
        try:
            amount = float(cb.get("amount", 0))
        except Exception:
            continue
        creation_time = None
        for field in ("last_modified_time", "created_at", "created_time"):
            if cb.get(field):
                creation_time = parse_iso_timestamp(cb[field])
                if creation_time is not None:
                    break
        for claimant in cb.get("claimants", []):
            if claimant.get("destination") != address:
                continue
            unlock_times = find_time_conditions(claimant.get("predicate", {}), creation_time)
            future_times = [ts for ts in unlock_times if ts > now]
            if future_times:
                lockups.append({
                    "id": cb.get("id", ""),
                    "amount": amount,
                    "unlock_timestamp": min(future_times),
                })
    unique = {}
    for x in lockups:
        unique[x["id"] or f'{x["amount"]}-{x["unlock_timestamp"]}'] = x
    return sorted(unique.values(), key=lambda x: x["unlock_timestamp"])

def check_wallet(address):
    account = get_account(address)
    if account is None:
        return None
    available = 0.0
    for b in account.get("balances", []):
        if b.get("asset_type") == "native":
            available = float(b.get("balance", 0))
            break
    lockups = get_lockups(address)
    return {
        "available": available,
        "locked": sum(x["amount"] for x in lockups),
        "next_unlock": lockups[0]["unlock_timestamp"] if lockups else None,
    }

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault("wallets", {})
    state.setdefault("unlock_alerts", {})
    return state

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)

def send_email(subject, body):
    sender = os.environ["ALERT_EMAIL_FROM"]
    recipient = os.environ["ALERT_EMAIL_TO"]
    app_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(sender, app_password)
        smtp.send_message(msg)

def clean_wallet_file(path):
    df = pd.read_csv(path)
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "address" not in df.columns:
        raise ValueError(f"{path}: missing address column")
    if "name" not in df.columns:
        df["name"] = [f"Wallet {i+1}" for i in range(len(df))]
    df["address"] = df["address"].fillna("").astype(str).str.strip().str.upper()
    df["name"] = df["name"].fillna("").astype(str).str.strip()
    df = df[df["address"].str.startswith("G") & (df["address"].str.len() == 56)]
    return df.drop_duplicates(subset=["address"])

def main():
    state = load_state()
    now = datetime.now(timezone.utc)
    balance_alerts = []
    unlock_alerts = []
    checked = errors = baselines = 0

    for group, csv_file in WALLET_GROUPS.items():
        if not os.path.exists(csv_file):
            print(f"Skipping missing file: {csv_file}")
            continue

        for _, row in clean_wallet_file(csv_file).iterrows():
            checked += 1
            name, address = row["name"], row["address"]
            try:
                result = check_wallet(address)
                if result is None:
                    print(f"{group}/{name}: not found")
                    continue

                current = round(float(result["available"]), 7)
                previous_record = state["wallets"].get(address)

                # First successful observation is baseline only.
                if previous_record is None:
                    baselines += 1
                else:
                    previous = float(previous_record.get("available", 0))
                    if current > previous + 0.0000001:
                        balance_alerts.append(
                            f"Group: {group}\nWallet: {name}\n"
                            f"Address: {address[:8]}...{address[-6:]}\n"
                            f"Previous: {previous:.7f} Pi\nCurrent: {current:.7f} Pi\n"
                            f"Increase: +{current-previous:.7f} Pi"
                        )

                unlock_ts = result.get("next_unlock")
                if unlock_ts:
                    seconds_left = unlock_ts - int(now.timestamp())
                    # Alert once when the unlock enters the 24-to-48-hour window.
                    if 24 * 3600 <= seconds_left < 48 * 3600:
                        event_key = f"{address}:{unlock_ts}"
                        if event_key not in state["unlock_alerts"]:
                            unlock_dt = datetime.fromtimestamp(unlock_ts, tz=timezone.utc)
                            unlock_alerts.append(
                                f"Group: {group}\nWallet: {name}\n"
                                f"Address: {address[:8]}...{address[-6:]}\n"
                                f"Locked: {result['locked']:.7f} Pi\n"
                                f"Unlock: {unlock_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}"
                            )
                            state["unlock_alerts"][event_key] = now.isoformat()

                state["wallets"][address] = {
                    "available": current,
                    "group": group,
                    "name": name,
                    "last_checked": now.isoformat(),
                    "next_unlock": unlock_ts,
                }
                print(f"{group}/{name}: {current:.7f} Pi")
                time.sleep(0.10)
            except Exception as e:
                errors += 1
                print(f"ERROR {group}/{name}: {e}")

    sections = []
    if balance_alerts:
        sections.append("PI BALANCE INCREASE ALERTS\n\n" + "\n\n----------------\n\n".join(balance_alerts))
    if unlock_alerts:
        sections.append("PI UNLOCK ALERTS - APPROXIMATELY ONE DAY REMAINING\n\n" +
                        "\n\n----------------\n\n".join(unlock_alerts))

    if sections:
        total = len(balance_alerts) + len(unlock_alerts)
        send_email(f"Pi Wallet Alert - {total} event(s)", "\n\n====================\n\n".join(sections))
        print(f"Email sent with {total} event(s).")
    else:
        print("No alert events this run.")

    save_state(state)
    print(f"Done. checked={checked}, baselines={baselines}, errors={errors}")

if __name__ == "__main__":
    main()
