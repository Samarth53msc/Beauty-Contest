# app.py
# Beauty Contest: commit -> reveal -> results (Streamlit)

import csv
import hashlib
import io
from datetime import datetime, timezone
from statistics import mean

import pandas as pd
import requests
import streamlit as st

APP_NAME = "Beauty Contest"

# Apps Script web app URL (ends with /exec)
API_URL = "https://script.google.com/macros/s/REPLACE_WITH_YOUR_/exec"

K_FACTOR = 2 / 3
ALLOWED_MIN, ALLOWED_MAX = 0, 100
MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_ts(ts: str):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_csv(url: str):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def post_json(api: str, payload: dict):
    try:
        r = requests.post(api, json=payload, timeout=15)
        return r.status_code, r.text
    except Exception as e:
        return None, f"Network error: {e}"


def to_csv_bytes(rows, fieldnames) -> bytes:
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=fieldnames)
    wr.writeheader()
    for r in rows:
        wr.writerow({k: r.get(k, "") for k in fieldnames})
    return buf.getvalue().encode("utf-8")


# ----------------------------------------------------------------------------
# Consensus engine (latest reveal + latest prior commit, hash + range check)
# ----------------------------------------------------------------------------
def run_consensus(api: str):
    commits_raw = get_csv(f"{api}?table=commits")
    reveals_raw = get_csv(f"{api}?table=reveals")

    commits = {}
    for r in commits_raw:
        uid = (r.get("uni_id") or "").strip()
        ch = (r.get("commit") or "").strip()
        ts = (r.get("timestamp_utc") or "").strip()
        if not uid or not ch:
            continue
        commits.setdefault(uid, []).append(
            {"timestamp_utc": ts, "ts_parsed": parse_ts(ts), "uni_id": uid, "commit": ch}
        )

    reveals = {}
    for r in reveals_raw:
        uid = (r.get("uni_id") or "").strip()
        num = (r.get("number") or "").strip()
        nonce = (r.get("nonce") or "").strip()
        ts = (r.get("timestamp_utc") or "").strip()
        if not uid or not num or not nonce:
            continue
        reveals.setdefault(uid, []).append(
            {"timestamp_utc": ts, "ts_parsed": parse_ts(ts), "uni_id": uid,
             "number_raw": num, "nonce": nonce}
        )

    verified_rows, matched_commits = [], {}

    for uid, rlist in reveals.items():
        latest_rev = sorted(rlist, key=lambda x: x["ts_parsed"] or MIN_DT)[-1]
        ts_rev = latest_rev["ts_parsed"] or MIN_DT

        try:
            num = int(latest_rev["number_raw"])
        except Exception:
            num = None

        cands = [c for c in commits.get(uid, []) if (c["ts_parsed"] or MIN_DT) <= ts_rev]
        cands.sort(key=lambda x: x["ts_parsed"] or MIN_DT)
        chosen = cands[-1] if cands else None
        if chosen:
            matched_commits[uid] = chosen

        reason, verified = "ok", False
        if chosen is None:
            reason = "no_commit_before_reveal"
        elif num is None:
            reason = "number_not_int"
        elif not (ALLOWED_MIN <= num <= ALLOWED_MAX):
            reason = "out_of_range"
        else:
            verified = sha256(f"{uid}|{num}|{latest_rev['nonce']}") == chosen["commit"]
            if not verified:
                reason = "hash_mismatch"

        verified_rows.append({
            "uni_id": uid,
            "timestamp_utc": latest_rev["timestamp_utc"],
            "number": "" if num is None else str(num),
            "nonce": latest_rev["nonce"],
            "commit": chosen["commit"] if chosen else "",
            "preimage_hash": "" if num is None else sha256(f"{uid}|{num}|{latest_rev['nonce']}"),
            "verified": "True" if verified else "False",
            "reason": reason,
        })

    valid = [(r["uni_id"], int(r["number"])) for r in verified_rows
             if r["verified"] == "True" and r["number"] != ""]

    if valid:
        avg_val = mean(n for _, n in valid)
        target = K_FACTOR * avg_val
        dists = [(u, n, abs(n - target)) for u, n in valid]
        min_dist = min(d for _, _, d in dists)
        winners = [(u, n, d) for u, n, d in dists if abs(d - min_dist) < 1e-12]
    else:
        avg_val = target = min_dist = None
        winners = []

    for r in verified_rows:
        if r["verified"] == "True" and target is not None:
            r["distance"] = f"{abs(int(r['number']) - target):.6f}"
        else:
            r["distance"] = ""

    verified_rows.sort(key=lambda r: (r["verified"] != "True",
                                      float(r["distance"]) if r["distance"] else float("inf"),
                                      r["uni_id"]))

    return {
        "rows": verified_rows,
        "matched_commits": sorted(matched_commits.values(),
                                  key=lambda r: (r["uni_id"], r["timestamp_utc"])),
        "valid": valid, "avg": avg_val, "target": target,
        "min_dist": min_dist, "winners": winners,
    }


def results_txt(res) -> str:
    lines = ["=== CLASS RESULTS (Latest-Attempt Consensus) ===", f"k_factor = {K_FACTOR}"]
    if res["valid"]:
        lines += [
            f"participants_counted = {len(res['valid'])}",
            f"average = {res['avg']:.6f}",
            f"target = {res['target']:.6f}",
            f"min_distance = {res['min_dist']:.6f}",
            "winners:",
        ]
        lines += [f"  - {u} with {n} (distance {d:.6f})" for u, n, d in res["winners"]]
    else:
        lines.append("No valid verified reveals. Cannot compute winners.")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------
st.set_page_config(page_title=APP_NAME, page_icon="🎯", layout="centered")
st.title(APP_NAME)

tab_commit, tab_reveal, tab_results = st.tabs(["Commit", "Reveal", "Results"])

# ---------------------------- COMMIT ----------------------------
with tab_commit:
    st.subheader("Commit")
    st.write("Pick a number (0–100) and a secret nonce. Only the hash is submitted. "
             "Save the preimage: you need it to reveal.")

    with st.form("commit_form"):
        c_uid = st.text_input("University ID")
        c_num = st.number_input("Your number (0..100)", min_value=ALLOWED_MIN,
                                max_value=ALLOWED_MAX, value=0, step=1)
        c_nonce = st.text_input("Secret nonce (keep it safe)")
        c_submit = st.form_submit_button("Submit commit")

    if c_submit:
        uid, nonce = c_uid.strip(), c_nonce.strip()
        if not uid:
            st.error("University ID cannot be empty.")
        elif not nonce:
            st.error("Nonce cannot be empty.")
        else:
            preimage = f"{uid}|{int(c_num)}|{nonce}"
            commit_hash = sha256(preimage)
            st.write("**Commit hash:**")
            st.code(commit_hash)
            st.write("**Preimage (write this down):**")
            st.code(preimage)
            status, text = post_json(API_URL, {"kind": "commit", "uni_id": uid,
                                           "commit": commit_hash})
            if status is not None and status < 400:
                st.success(f"Server: {status} {text}")
            else:
                st.error(f"Server: {status} {text}")

# ---------------------------- REVEAL ----------------------------
with tab_reveal:
    st.subheader("Reveal")
    st.write("Re-enter EXACTLY what you committed.")

    with st.form("reveal_form"):
        r_uid = st.text_input("University ID", key="r_uid")
        r_num = st.number_input("Your number (0..100)", min_value=ALLOWED_MIN,
                                max_value=ALLOWED_MAX, value=0, step=1, key="r_num")
        r_nonce = st.text_input("Your secret nonce", key="r_nonce")
        r_submit = st.form_submit_button("Submit reveal")

    if r_submit:
        uid, nonce = r_uid.strip(), r_nonce.strip()
        if not uid:
            st.error("University ID cannot be empty.")
        elif not nonce:
            st.error("Nonce cannot be empty.")
        else:
            status, text = post_json(API_URL, {"kind": "reveal", "uni_id": uid,
                                           "number": int(r_num), "nonce": nonce})
            if status is not None and status < 400:
                st.success(f"Server: {status} {text}")
            else:
                st.error(f"Server: {status} {text}")

# ---------------------------- RESULTS ----------------------------
with tab_results:
    st.subheader("Results")
    st.write(f"Target = {K_FACTOR:.4f} × class average. Winner(s): minimal distance to target.")

    if st.button("Compute results"):
        try:
            st.session_state["res"] = run_consensus(API_URL)
        except Exception as e:
            st.session_state.pop("res", None)
            st.error(f"Could not fetch/compute results: {e}")

    res = st.session_state.get("res")
    if res:
        if res["valid"]:
            c1, c2, c3 = st.columns(3)
            c1.metric("Participants", len(res["valid"]))
            c2.metric("Average", f"{res['avg']:.4f}")
            c3.metric("Target", f"{res['target']:.4f}")
            st.success("Winner(s): " + ", ".join(
                f"{u} ({n}, distance {d:.4f})" for u, n, d in res["winners"]))
        else:
            st.warning("No valid verified reveals. Cannot compute winners.")

        st.write("**Leaderboard**")
        lb_cols = ["uni_id", "number", "verified", "reason", "distance",
                   "commit", "nonce", "timestamp_utc"]
        st.dataframe(pd.DataFrame(res["rows"], columns=lb_cols), use_container_width=True)

        st.download_button("Download class_leaderboard.csv",
                           to_csv_bytes(res["rows"], lb_cols),
                           "class_leaderboard.csv", "text/csv")
        st.download_button("Download merged_commits.csv",
                           to_csv_bytes(res["matched_commits"],
                                        ["timestamp_utc", "uni_id", "commit"]),
                           "merged_commits.csv", "text/csv")
        st.download_button("Download merged_reveals.csv",
                           to_csv_bytes([r for r in res["rows"] if r["verified"] == "True"],
                                        ["timestamp_utc", "uni_id", "number", "nonce",
                                         "preimage_hash", "verified"]),
                           "merged_reveals.csv", "text/csv")
        st.download_button("Download class_results.txt", results_txt(res),
                           "class_results.txt", "text/plain")
