import os
import time
import requests
import pandas as pd
import streamlit as st

from datetime import datetime, timezone


# ============================================================
# CONFIGURATION
# ============================================================

HORIZON_URL = "https://api.mainnet.minepi.com"

DASHBOARDS = {
    "WC": {"csv": "pi_wallets_wc.csv", "colors": False},
    "WCD": {"csv": "pi_wallets_wcd.csv", "colors": False},
    "RP": {"csv": "pi_wallets_rp.csv", "colors": False},
    "LP": {"csv": "pi_wallets_lp.csv", "colors": True},
    "Pi done by me and others": {"csv": "pi_wallets_donebymeothers.csv", "colors": False},
}

# ============================================================
# STREAMLIT PAGE
# ============================================================

st.set_page_config(
    page_title="Pi Wallet Monitor",
    page_icon="π",
    layout="wide"
)


# Select which wallet list/dashboard to run from one application.
st.sidebar.title("Pi Wallet Dashboards")
SELECTED_DASHBOARD = st.sidebar.radio(
    "Select dashboard",
    list(DASHBOARDS.keys()),
    index=0,
)
CSV_FILE = DASHBOARDS[SELECTED_DASHBOARD]["csv"]
USE_UNLOCK_COLORS = DASHBOARDS[SELECTED_DASHBOARD]["colors"]



# ============================================================
# HELPER: ISO TIME -> UNIX TIMESTAMP
# ============================================================

def parse_iso_timestamp(value):

    try:

        value = str(value)

        dt = datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00"
            )
        )

        return int(
            dt.timestamp()
        )

    except Exception:

        return None


# ============================================================
# HELPER: UNIX TIMESTAMP -> READABLE DATE
# ============================================================

def timestamp_to_date(timestamp):

    try:

        dt = datetime.fromtimestamp(
            int(timestamp),
            tz=timezone.utc
        )

        return dt.strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )

    except Exception:

        return "Unknown"


# ============================================================
# DASHBOARD COLOR HIGHLIGHTING BY UNLOCK DATE
# ============================================================

def get_unlock_year_month(value):
    """
    Extract (year, month) from values such as:
    2027-04-15 10:00:00 UTC

    Returns (None, None) when there is no valid unlock date.
    """
    if value is None:
        return None, None

    value = str(value).strip()

    if not value or value in ("-", "None", "Unknown", "NaT"):
        return None, None

    try:
        dt = datetime.strptime(value[:10], "%Y-%m-%d")
        return dt.year, dt.month
    except Exception:
        return None, None


def get_unlock_color(value):
    """
    Color rules:

    2026:
        All months -> Pink

    2027:
        Jan-Mar -> Orange
        Apr-Jul -> Off white
        Aug-Nov -> Green
        Dec -> Sky blue

    2028:
        Jan-Mar -> Gray
        Apr-Jul -> Cream
        Aug-Nov -> Brown
        Dec -> Yellow
    """
    year, month = get_unlock_year_month(value)

    if year == 2026:
        return "#FFB6C1"  # Pink

    if year == 2027:
        if 1 <= month < 4:
            return "#FFA500"  # Orange
        elif 4 <= month < 8:
            return "#FAF9F6"  # Off white
        elif 8 <= month < 12:
            return "#90EE90"  # Green
        elif month == 12:
            return "#87CEEB"  # Sky blue

    if year == 2028:
        if 1 <= month < 4:
            return "#BEBEBE"  # Gray
        elif 4 <= month < 8:
            return "#FFFDD0"  # Cream
        elif 8 <= month < 12:
            return "#D2B48C"  # Brown / tan for readable dashboard
        elif month == 12:
            return "#FFF176"  # Yellow

    return None


def highlight_wallet_unlock_row(row):
    color = get_unlock_color(row.get("Next Unlock"))

    if color:
        return [f"background-color: {color}; color: black;"] * len(row)

    return [""] * len(row)


def highlight_lockup_unlock_row(row):
    color = get_unlock_color(row.get("Unlock Date"))

    if color:
        return [f"background-color: {color}; color: black;"] * len(row)

    return [""] * len(row)


# ============================================================
# EXTRACT LOCKUP TIME CONDITIONS
# ============================================================

def find_time_conditions(
    predicate,
    creation_time=None,
    negated=False
):

    """
    Recursively parses Stellar/Pi style claim predicates.

    Possible formats include:

    {
        "not": {
            "abs_before": "..."
        }
    }

    {
        "not": {
            "rel_before": "..."
        }
    }

    Also handles camelCase variants.
    """

    unlock_times = []

    if not isinstance(
        predicate,
        dict
    ):

        return unlock_times


    # ========================================================
    # NOT
    # ========================================================

    if "not" in predicate:

        unlock_times.extend(

            find_time_conditions(

                predicate[
                    "not"
                ],

                creation_time,

                not negated
            )
        )


    # ========================================================
    # ABSOLUTE BEFORE
    # ========================================================

    absolute_keys = [

        "abs_before",

        "absBefore",

        "abs_before_epoch",

        "absBeforeEpoch"
    ]


    for key in absolute_keys:

        if key not in predicate:

            continue


        value = predicate[
            key
        ]


        timestamp = None


        # Try integer timestamp first
        try:

            timestamp = int(
                value
            )

        except Exception:

            timestamp = None


        # Try ISO timestamp
        if timestamp is None:

            timestamp = parse_iso_timestamp(
                value
            )


        # A NOT(before X) condition means:
        # claimable AFTER X
        if (

            timestamp is not None

            and

            negated

        ):

            unlock_times.append(
                timestamp
            )


    # ========================================================
    # RELATIVE BEFORE
    # ========================================================

    relative_keys = [

        "rel_before",

        "relBefore"
    ]


    for key in relative_keys:

        if key not in predicate:

            continue


        try:

            seconds = int(
                predicate[
                    key
                ]
            )


            if (

                creation_time
                is not None

                and

                negated

            ):

                unlock_timestamp = (

                    creation_time

                    +

                    seconds
                )


                unlock_times.append(
                    unlock_timestamp
                )


        except Exception:

            pass


    # ========================================================
    # AND / OR EXPRESSIONS
    # ========================================================

    for logical_key in [

        "and",

        "or"

    ]:

        if logical_key not in predicate:

            continue


        children = predicate[
            logical_key
        ]


        if isinstance(
            children,
            list
        ):

            for child in children:

                unlock_times.extend(

                    find_time_conditions(

                        child,

                        creation_time,

                        negated
                    )
                )


    return unlock_times


# ============================================================
# GET NORMAL ACCOUNT
# ============================================================

def get_account(address):

    url = (
        f"{HORIZON_URL}"
        f"/accounts/"
        f"{address}"
    )


    response = requests.get(

        url,

        timeout=30,

        headers={
            "Accept":
                "application/json"
        }
    )


    if response.status_code == 404:

        return None


    response.raise_for_status()


    return response.json()


# ============================================================
# GET CLAIMABLE BALANCES
# ============================================================

def get_claimable_balances(address):

    url = (
        f"{HORIZON_URL}"
        f"/claimable_balances"
    )


    params = {

        "claimant":
            address,

        "limit":
            200
    }


    response = requests.get(

        url,

        params=params,

        timeout=30,

        headers={
            "Accept":
                "application/json"
        }
    )


    response.raise_for_status()


    data = response.json()


    records = (

        data

        .get(
            "_embedded",
            {}
        )

        .get(
            "records",
            []
        )
    )


    return records


# ============================================================
# GET WALLET LOCKUPS
# ============================================================

def get_lockups(address):

    claimables = (
        get_claimable_balances(
            address
        )
    )


    now = int(

        datetime.now(
            timezone.utc
        ).timestamp()
    )


    lockups = []


    for cb in claimables:

        # ====================================================
        # ASSET
        # ====================================================

        asset = cb.get(
            "asset",
            "native"
        )


        # Only native Pi
        if asset != "native":

            continue


        # ====================================================
        # AMOUNT
        # ====================================================

        try:

            amount = float(
                cb.get(
                    "amount",
                    0
                )
            )

        except Exception:

            continue


        # ====================================================
        # CLAIMABLE BALANCE ID
        # ====================================================

        balance_id = cb.get(
            "id",
            ""
        )


        # ====================================================
        # CREATION / MODIFICATION TIME
        # ====================================================

        creation_time = None


        possible_time_fields = [

            "last_modified_time",

            "created_at",

            "created_time"
        ]


        for field in possible_time_fields:

            if cb.get(
                field
            ):

                creation_time = (
                    parse_iso_timestamp(
                        cb[
                            field
                        ]
                    )
                )

                if (
                    creation_time
                    is not None
                ):

                    break


        # ====================================================
        # CLAIMANTS
        # ====================================================

        claimants = cb.get(
            "claimants",
            []
        )


        for claimant in claimants:

            destination = (
                claimant.get(
                    "destination"
                )
            )


            if destination != address:

                continue


            predicate = (
                claimant.get(
                    "predicate",
                    {}
                )
            )


            # =================================================
            # EXTRACT POSSIBLE UNLOCK TIMES
            # =================================================

            unlock_times = (

                find_time_conditions(

                    predicate,

                    creation_time
                )
            )


            # =================================================
            # FUTURE UNLOCK TIMES ONLY
            # =================================================

            future_times = [

                timestamp

                for timestamp

                in unlock_times

                if timestamp > now
            ]


            if not future_times:

                continue


            # Earliest future unlock condition
            unlock_timestamp = min(
                future_times
            )


            lockups.append({

                "balance_id":
                    balance_id,

                "amount":
                    amount,

                "unlock_timestamp":
                    unlock_timestamp,

                "unlock_date":
                    timestamp_to_date(
                        unlock_timestamp
                    ),

                "predicate":
                    predicate
            })


    # ========================================================
    # REMOVE DUPLICATES
    # ========================================================

    unique = {}


    for lock in lockups:

        balance_id = lock[
            "balance_id"
        ]


        if balance_id:

            unique[
                balance_id
            ] = lock

        else:

            key = (

                lock[
                    "amount"
                ],

                lock[
                    "unlock_timestamp"
                ]
            )


            unique[
                str(key)
            ] = lock


    lockups = list(
        unique.values()
    )


    # Sort nearest unlock first
    lockups.sort(

        key=lambda x:
            x[
                "unlock_timestamp"
            ]
    )


    return lockups


# ============================================================
# CHECK ONE WALLET
# ============================================================

def check_wallet(
    name,
    address
):

    result = {

        "name":
            name,

        "address":
            address,

        "status":
            "UNKNOWN",

        "available":
            0.0,

        "locked":
            0.0,

        "total":
            0.0,

        "next_unlock":
            None,

        "lockups":
            [],

        "claimable_count":
            0,

        "error":
            None
    }


    try:

        # ====================================================
        # ACCOUNT
        # ====================================================

        account = get_account(
            address
        )


        if account is None:

            result[
                "status"
            ] = "NOT FOUND"

            return result


        # ====================================================
        # AVAILABLE BALANCE
        # ====================================================

        available = 0.0


        balances = account.get(
            "balances",
            []
        )


        for balance in balances:

            if (

                balance.get(
                    "asset_type"
                )

                ==

                "native"

            ):

                available = float(

                    balance.get(
                        "balance",
                        0
                    )
                )

                break


        # ====================================================
        # LOCKUPS
        # ====================================================

        active_lockups = (
            get_lockups(
                address
            )
        )


        locked = sum(

            lock[
                "amount"
            ]

            for lock

            in active_lockups
        )


        # ====================================================
        # NEXT UNLOCK
        # ====================================================

        next_unlock = None


        if active_lockups:

            next_unlock = (

                active_lockups[0]
                [
                    "unlock_date"
                ]
            )


        # ====================================================
        # TOTAL
        # ====================================================

        total = (

            available

            +

            locked
        )


        result.update({

            "status":
                "OK",

            "available":
                available,

            "locked":
                locked,

            "total":
                total,

            "next_unlock":
                next_unlock,

            "lockups":
                active_lockups,

            "claimable_count":
                len(
                    active_lockups
                )
        })


        return result


    except Exception as e:

        result[
            "status"
        ] = "ERROR"


        result[
            "error"
        ] = str(e)


        return result


# ============================================================
# HEADER
# ============================================================

st.title(
    f"π Pi Network Wallet Monitor — {SELECTED_DASHBOARD}"
)


st.caption(
    "Available Pi, locked Pi and lockup dates "
    "using public wallet addresses."
)


st.warning(

    "Use PUBLIC wallet addresses only. "
    "Never place Pi wallet passphrases or "
    f"private keys in {CSV_FILE}."
)


st.divider()


# ============================================================
# CHECK CSV EXISTS
# ============================================================

if not os.path.exists(
    CSV_FILE
):

    st.error(

        f"{CSV_FILE} was not found."
    )


    st.info(

        f"Place {CSV_FILE} in the same folder as pi_dashboard_all.py."
    )


    st.stop()


# ============================================================
# LOAD CSV
# ============================================================

try:

    wallet_df = pd.read_csv(
        CSV_FILE
    )


except Exception as e:

    st.error(

        f"Could not read "
        f"{CSV_FILE}: {e}"
    )


    st.stop()


# ============================================================
# NORMALIZE COLUMN NAMES
# ============================================================

wallet_df.columns = [

    str(column)
    .strip()
    .lower()

    for column

    in wallet_df.columns
]


# ============================================================
# REQUIRE ADDRESS COLUMN
# ============================================================

if "address" not in wallet_df.columns:

    st.error(

        "Your CSV must contain "
        "a column named 'address'."
    )


    st.stop()


# ============================================================
# CREATE NAME COLUMN IF MISSING
# ============================================================

if "name" not in wallet_df.columns:

    wallet_df[
        "name"
    ] = [

        f"Wallet {i + 1}"

        for i

        in range(
            len(wallet_df)
        )
    ]


# ============================================================
# CLEAN ADDRESS COLUMN
# ============================================================

wallet_df[
    "address"
] = (

    wallet_df[
        "address"
    ]

    .fillna("")

    .astype(str)

    .str.strip()

    .str.upper()
)


# ============================================================
# CLEAN NAME COLUMN
# ============================================================

wallet_df[
    "name"
] = (

    wallet_df[
        "name"
    ]

    .fillna("")

    .astype(str)

    .str.strip()
)


# ============================================================
# DEFAULT NAMES
# ============================================================

for i in range(
    len(wallet_df)
):

    if not wallet_df.loc[
        i,
        "name"
    ]:

        wallet_df.loc[
            i,
            "name"
        ] = (
            f"Wallet {i + 1}"
        )


# ============================================================
# REMOVE EMPTY ADDRESSES
# ============================================================

wallet_df = wallet_df[

    wallet_df[
        "address"
    ] != ""

].copy()


original_count = len(
    wallet_df
)


# ============================================================
# REMOVE DUPLICATES
# ============================================================

wallet_df = (

    wallet_df

    .drop_duplicates(

        subset=[
            "address"
        ]
    )

    .reset_index(
        drop=True
    )
)


duplicates_removed = (

    original_count

    -

    len(wallet_df)
)


# ============================================================
# VALIDATE PI ADDRESS
# ============================================================

valid_wallets = []

invalid_wallets = []


for _, row in wallet_df.iterrows():

    name = row[
        "name"
    ]


    address = row[
        "address"
    ]


    # Stellar/Pi public address:
    # G + total 56 characters

    valid = (

        address.startswith(
            "G"
        )

        and

        len(address) == 56
    )


    if valid:

        valid_wallets.append({

            "name":
                name,

            "address":
                address
        })


    else:

        invalid_wallets.append({

            "name":
                name,

            "address":
                address
        })


# ============================================================
# CSV SUMMARY
# ============================================================

st.subheader(
    "Wallet File"
)


c1, c2, c3 = st.columns(
    3
)


c1.metric(
    "Wallets in CSV",
    original_count
)


c2.metric(
    "Valid Wallets",
    len(
        valid_wallets
    )
)


c3.metric(
    "Duplicates Removed",
    duplicates_removed
)


if invalid_wallets:

    st.warning(

        f"{len(invalid_wallets)} "
        f"invalid wallet address(es) "
        f"were skipped."
    )


    with st.expander(
        "Show Invalid Addresses"
    ):

        st.dataframe(

            pd.DataFrame(
                invalid_wallets
            ),

            hide_index=True,

            use_container_width=True
        )


if not valid_wallets:

    st.error(
        "No valid wallets found."
    )

    st.stop()


st.divider()


# ============================================================
# CHECK BUTTON
# ============================================================

col_button, col_text = (
    st.columns(
        [1, 5]
    )
)


with col_button:

    check_button = st.button(

        "🔄 Check Wallets",

        type="primary"
    )


with col_text:

    st.write(

        f"{len(valid_wallets)} "
        f"wallet(s) ready."
    )


if not check_button:

    st.info(

        "Click 'Check Wallets' "
        "to retrieve balances."
    )

    st.stop()


# ============================================================
# PROCESS ALL WALLETS
# ============================================================

results = []


progress = st.progress(
    0
)


status_message = st.empty()


wallet_count = len(
    valid_wallets
)


for i, wallet in enumerate(
    valid_wallets
):

    status_message.write(

        f"Checking "
        f"{i + 1} of "
        f"{wallet_count}: "
        f"{wallet['name']}"
    )


    result = check_wallet(

        wallet[
            "name"
        ],

        wallet[
            "address"
        ]
    )


    results.append(
        result
    )


    progress.progress(

        (i + 1)

        /

        wallet_count
    )


    # Small API delay
    time.sleep(
        0.05
    )


progress.empty()

status_message.empty()


# ============================================================
# CALCULATE TOTALS
# ============================================================

found_wallets = sum(

    1

    for wallet

    in results

    if wallet[
        "status"
    ] == "OK"
)


available_total = sum(

    wallet[
        "available"
    ]

    for wallet

    in results

    if wallet[
        "status"
    ] == "OK"
)


locked_total = sum(

    wallet[
        "locked"
    ]

    for wallet

    in results

    if wallet[
        "status"
    ] == "OK"
)


total_pi = (

    available_total

    +

    locked_total
)


not_found_count = sum(

    1

    for wallet

    in results

    if wallet[
        "status"
    ] == "NOT FOUND"
)


error_count = sum(

    1

    for wallet

    in results

    if wallet[
        "status"
    ] == "ERROR"
)


# ============================================================
# SUMMARY
# ============================================================

st.subheader(
    "Portfolio Summary"
)


c1, c2, c3, c4 = st.columns(
    4
)


c1.metric(

    "Found Wallets",

    f"{found_wallets}"
    f"/"
    f"{wallet_count}"
)


c2.metric(

    "Available Pi",

    f"{available_total:,.4f}"
)


c3.metric(

    "Locked Pi",

    f"{locked_total:,.4f}"
)


c4.metric(

    "Total Pi",

    f"{total_pi:,.4f}"
)


if (

    not_found_count > 0

    or

    error_count > 0

):

    st.caption(

        f"Not found: "
        f"{not_found_count} | "
        f"Errors: "
        f"{error_count}"
    )


st.divider()


# ============================================================
# WALLET RESULTS TABLE
# ============================================================

st.subheader(
    "Wallet Balances"
)


rows = []


for wallet in results:

    rows.append({

        "Wallet":
            wallet[
                "name"
            ],

        "Address":
            wallet[
                "address"
            ],

        "Status":
            wallet[
                "status"
            ],

        "Available Pi":
            round(
                wallet[
                    "available"
                ],
                4
            ),

        "Locked Pi":
            round(
                wallet[
                    "locked"
                ],
                4
            ),

        "Lockups":
            wallet[
                "claimable_count"
            ],

        "Next Unlock":
            (
                wallet[
                    "next_unlock"
                ]

                if wallet[
                    "next_unlock"
                ]

                else "-"
            ),

        "Total Pi":
            round(
                wallet[
                    "total"
                ],
                4
            )
    })


results_df = pd.DataFrame(
    rows
)


# LP keeps the unlock-date color highlighting from the original LP script.
display_results_df = (
    results_df.style.apply(highlight_wallet_unlock_row, axis=1)
    if USE_UNLOCK_COLORS
    else results_df
)

st.dataframe(

    display_results_df,

    hide_index=True,

    use_container_width=True
)

# Color legend (LP only)
if USE_UNLOCK_COLORS:
    st.markdown(
        """
        **Unlock date colors:**  
        <span style="background-color:#FFB6C1; color:black; padding:3px 8px; border-radius:4px;">2026: Pink</span>
        &nbsp;
        <span style="background-color:#FFA500; color:black; padding:3px 8px; border-radius:4px;">2027 Jan-Mar: Orange</span>
        &nbsp;
        <span style="background-color:#FAF9F6; color:black; padding:3px 8px; border:1px solid #ccc; border-radius:4px;">2027 Apr-Jul: Off white</span>
        &nbsp;
        <span style="background-color:#90EE90; color:black; padding:3px 8px; border-radius:4px;">2027 Aug-Nov: Green</span>
        &nbsp;
        <span style="background-color:#87CEEB; color:black; padding:3px 8px; border-radius:4px;">2027 Dec: Sky blue</span>
        <br><br>
        <span style="background-color:#BEBEBE; color:black; padding:3px 8px; border-radius:4px;">2028 Jan-Mar: Gray</span>
        &nbsp;
        <span style="background-color:#FFFDD0; color:black; padding:3px 8px; border:1px solid #ccc; border-radius:4px;">2028 Apr-Jul: Cream</span>
        &nbsp;
        <span style="background-color:#D2B48C; color:black; padding:3px 8px; border-radius:4px;">2028 Aug-Nov: Brown</span>
        &nbsp;
        <span style="background-color:#FFF176; color:black; padding:3px 8px; border-radius:4px;">2028 Dec: Yellow</span>
        """,
        unsafe_allow_html=True
    )



# ============================================================
# DOWNLOAD WALLET RESULTS
# ============================================================

results_csv = (

    results_df

    .to_csv(
        index=False
    )

    .encode(
        "utf-8"
    )
)


st.download_button(

    label=
        "⬇️ Download Wallet Results",

    data=
        results_csv,

    file_name=
        f"pi_wallet_results_{SELECTED_DASHBOARD.lower()}.csv",

    mime=
        "text/csv"
)


# ============================================================
# LOCKUP DETAILS
# ============================================================

st.divider()


st.subheader(
    "🔒 Active Lockups"
)


lockup_rows = []


for wallet in results:

    for number, lock in enumerate(

        wallet[
            "lockups"
        ],

        start=1
    ):

        lockup_rows.append({

            "Wallet":
                wallet[
                    "name"
                ],

            "Address":
                wallet[
                    "address"
                ],

            "Lockup #":
                number,

            "Locked Pi":
                round(
                    lock[
                        "amount"
                    ],
                    4
                ),

            "Unlock Date":
                lock[
                    "unlock_date"
                ],

            "Balance ID":
                lock[
                    "balance_id"
                ]
        })


if lockup_rows:

    lockup_df = pd.DataFrame(
        lockup_rows
    )


    display_lockup_df = (
        lockup_df.style.apply(highlight_lockup_unlock_row, axis=1)
        if USE_UNLOCK_COLORS
        else lockup_df
    )

    st.dataframe(

        display_lockup_df,

        hide_index=True,

        use_container_width=True
    )


    lockup_csv = (

        lockup_df

        .to_csv(
            index=False
        )

        .encode(
            "utf-8"
        )
    )


    st.download_button(

        label=
            "⬇️ Download Lockup Details",

        data=
            lockup_csv,

        file_name=
            f"pi_lockup_details_{SELECTED_DASHBOARD.lower()}.csv",

        mime=
            "text/csv"
    )


else:

    st.info(
        "No active lockups were detected."
    )


# ============================================================
# ERRORS
# ============================================================

errors = [

    wallet

    for wallet

    in results

    if wallet[
        "status"
    ] == "ERROR"
]


if errors:

    st.divider()


    st.subheader(
        "⚠️ API Errors"
    )


    for wallet in errors:

        st.error(

            f"{wallet['name']} | "
            f"{wallet['address']} | "
            f"{wallet.get('error')}"
        )


# ============================================================
# DEBUG SECTION
# ============================================================

st.divider()


with st.expander(
    "Developer / Lockup Debug Information"
):

    st.write(
        "This section can help diagnose wallets "
        "whose Pi app shows a lockup but the "
        "dashboard does not detect it."
    )


    for wallet in results:

        st.markdown(
            f"### {wallet['name']}"
        )


        st.write(
            "Address:",
            wallet[
                "address"
            ]
        )


        st.write(
            "Detected active lockups:",
            len(
                wallet[
                    "lockups"
                ]
            )
        )


        if wallet[
            "lockups"
        ]:

            for lock in wallet[
                "lockups"
            ]:

                st.json({

                    "amount":
                        lock[
                            "amount"
                        ],

                    "unlock_date":
                        lock[
                            "unlock_date"
                        ],

                    "balance_id":
                        lock[
                            "balance_id"
                        ],

                    "predicate":
                        lock[
                            "predicate"
                        ]
                })


# ============================================================
# FOOTER
# ============================================================

st.divider()


st.caption(

    f"Source: {CSV_FILE} | "
    "Public-address-only blockchain monitoring. "
    "Do not store wallet passphrases/private keys "
    "in the CSV or Python script."
)