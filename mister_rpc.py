import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import paramiko
from pypresence import Presence

# ==============================================================================
# CONFIGURATION / PARAMETERS
# ==============================================================================
POLL_INTERVAL = 5
# ==============================================================================

# Global trackers for live change detection
last_known_earned = None

# Valid ROM & Arcade extensions that indicate an actual game is being played
VALID_ROM_EXTENSIONS = {
    ".nes", ".fds", ".sfc", ".smc", ".md", ".gen", ".sms", ".gg", 
    ".gb", ".gbc", ".gba", ".z64", ".v64", ".n64", ".pce", ".col", 
    ".a26", ".a52", ".a78", ".lnx", ".ws", ".wsc", ".ngp", ".ngc",
    ".d64", ".g64", ".t64", ".tap", ".tzx", ".m3u", ".cue", ".iso",
    ".mra", ".zip"
}


def fetch_latest_unlock_details(ra_user, ra_api_key, target_earned_count):
    """
    Retries fetching recent unlocks to ensure the RetroAchievements server 
    has finished indexing the newly earned achievement.
    """
    for attempt in range(3):
        try:
            if attempt > 0:
                time.sleep(2)

            url = f"https://retroachievements.org/API/API_GetUserRecentAchievements.php?u={urllib.parse.quote(ra_user)}&y={ra_api_key}&m=3"
            req = urllib.request.Request(url, headers={"User-Agent": "MiSTer-Discord-RPC"})

            with urllib.request.urlopen(req, timeout=5) as response:
                data = json.loads(response.read().decode())
                if isinstance(data, list) and len(data) > 0:
                    latest = data[0]
                    title = latest.get("Title", "")
                    
                    if title:
                        return {
                            "title": title,
                            "description": latest.get("Description", ""),
                            "game_title": latest.get("GameTitle", "Unknown Game")
                        }
        except Exception as e:
            print(f"[RA Debug Error] Retry {attempt + 1} failed to fetch unlock details: {e}")
    
    return None


def get_retroachievements_progress(ra_user, ra_api_key):
    """
    Queries RetroAchievements for live progress. Returns None if the game has no RA set,
    allowing the script to cleanly fallback to Core info.
    """
    global last_known_earned

    if not ra_user or not ra_api_key:
        return None

    try:
        url = f"https://retroachievements.org/API/API_GetUserRecentlyPlayedGames.php?u={urllib.parse.quote(ra_user)}&y={ra_api_key}&c=1"
        req = urllib.request.Request(url, headers={"User-Agent": "MiSTer-Discord-RPC"})

        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read().decode())
            if isinstance(data, list) and len(data) > 0:
                latest_game = data[0]

                game_title = latest_game.get("Title", "Unknown")
                game_id = latest_game.get("GameID")
                hardcore_earned = latest_game.get("NumAchievedHardcore", 0)
                softcore_earned = latest_game.get("NumAchieved", 0)
                total = latest_game.get("NumPossibleAchievements", 0)

                # If the game has no achievements on RA, return None
                if not total or total == 0 or not game_id:
                    return None

                is_hardcore = hardcore_earned > 0 or latest_game.get("LastPlayed", "") != ""
                mode_label = "Hardcore" if is_hardcore else "Softcore"
                earned = hardcore_earned if is_hardcore else softcore_earned
                percent = int((earned / total) * 100)

                # --- LIVE UNLOCK DETECTION WITH RETRY ---
                if last_known_earned is not None and earned > last_known_earned:
                    unlock_info = fetch_latest_unlock_details(ra_user, ra_api_key, earned)
                    
                    print("\n" + "=" * 60, flush=True)
                    print(f"🏆 NEW ACHIEVEMENT UNLOCKED IN [{game_title}]!", flush=True)
                    if unlock_info:
                        print(f"   Achievement: {unlock_info['title']}", flush=True)
                        if unlock_info['description']:
                            print(f"   Details:     {unlock_info['description']}", flush=True)
                    else:
                        print("   Achievement: [Details pending server index]", flush=True)
                    print(f"   Progress:    {earned} / {total} ({percent}%) [{mode_label}]", flush=True)
                    print("=" * 60 + "\n", flush=True)

                if last_known_earned is None:
                    print(f"[RA Connected] Tracking '{game_title}' | Baseline: {earned}/{total} ({percent}%) [{mode_label}]", flush=True)

                last_known_earned = earned

                return f"RA: {earned}/{total} ({percent}%) [{mode_label}]"
    except Exception as e:
        print(f"[RA Debug Error] Could not fetch recently played game: {e}", flush=True)

    return None


def parse_mister_path(raw_path):
    """Parses the full file path from MiSTer to determine Core, Ext, and Game Title."""
    if not raw_path:
        return "Menu", "", "At the Menu"

    core_name = "MiSTer FPGA"
    parts = raw_path.split("/")
    
    if "games" in parts:
        try:
            games_idx = parts.index("games")
            if len(parts) > games_idx + 1:
                core_name = parts[games_idx + 1]
        except ValueError:
            pass
    elif "_Arcade" in parts or "arcade" in raw_path.lower():
        core_name = "Arcade"
    elif len(parts) > 1 and parts[1] != "":
        core_name = parts[1]

    file_name = os.path.basename(raw_path)
    display_name = os.path.splitext(file_name)[0]
    ext = os.path.splitext(file_name)[1].lower()

    if "menu" in raw_path.lower() and ext not in VALID_ROM_EXTENSIONS:
        return core_name, "", "At the Menu"

    return core_name, ext, display_name


def get_mister_recent_activity(host, user, password):
    """Connects to MiSTer via SSH and reads current active game or recent logs."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=user, password=password, timeout=5)

        stdin, stdout, stderr = ssh.exec_command("cat /tmp/ACTIVEGAME 2>/dev/null")
        active_path = stdout.read().decode("utf-8").strip()

        if active_path:
            ssh.close()
            return active_path

        stdin, stdout, stderr = ssh.exec_command(
            "tail -n 1 /media/fat/config/recent.lst 2>/dev/null"
        )
        raw_path = stdout.read().decode("utf-8").strip()
        ssh.close()

        if not raw_path:
            return ""

        return raw_path

    except Exception as e:
        print(f"SSH Connection error: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="MiSTer FPGA Discord Rich Presence")
    parser.add_argument("--client-id", required=True, help="Discord Application Client ID")
    parser.add_argument("--host", required=True, help="MiSTer FPGA IP or hostname")
    parser.add_argument("--user", required=True, help="SSH Username for MiSTer")
    parser.add_argument("--password", required=True, help="SSH Password for MiSTer")
    parser.add_argument("--ra-user", required=True, help="RetroAchievements Username")
    parser.add_argument("--ra-key", required=True, help="RetroAchievements Web API Key")

    args = parser.parse_args()

    print(f"Connecting to MiSTer at {args.host}...")
    
    rpc = Presence(args.client_id)
    try:
        rpc.connect()
        print("Connected to Discord Rich Presence successfully!")
    except Exception as e:
        print(f"Failed to connect to Discord local client: {e}")
        return

    last_activity = None
    activity_start_time = int(time.time())

    try:
        while True:
            raw_path = get_mister_recent_activity(args.host, args.user, args.password)
            core_name, ext, display_name = parse_mister_path(raw_path)

            current_state_key = raw_path if raw_path else "menu"

            # Check if we are at a menu or core browser
            is_at_menu = (
                not raw_path or 
                ("menu" in raw_path.lower() and ext not in VALID_ROM_EXTENSIONS) or 
                ext not in VALID_ROM_EXTENSIONS or 
                display_name == "At the Menu"
            )

            if is_at_menu:
                if core_name != "Menu" and core_name != "MiSTer FPGA":
                    menu_details = f"Core: {core_name}"
                    menu_state = "Browsing Library..."
                else:
                    menu_details = "System Menu"
                    menu_state = "At the main UI"
            else:
                # Bypasses RA check if playing on Arcade core since Arcade has no RA support
                if core_name.lower() == "arcade":
                    ra_status = None
                else:
                    ra_status = get_retroachievements_progress(args.ra_user, args.ra_key)
                
                bottom_line = ra_status if ra_status else f"Core: {core_name}"

            # If the game or menu state changed, reset the elapsed timer baseline
            if current_state_key != last_activity:
                print(f"\n[MiSTer Event] Status Changed -> Core: {core_name} | Game/Menu: {display_name}")
                global last_known_earned
                last_known_earned = None
                activity_start_time = int(time.time())
                last_activity = current_state_key

            try:
                if is_at_menu:
                    rpc.update(
                        details=menu_details,
                        state=menu_state,
                        large_image="mister_logo",
                        large_text="MiSTer FPGA",
                        start=activity_start_time,
                    )
                else:
                    rpc.update(
                        details=f"Playing {display_name}",
                        state=bottom_line,
                        large_image="mister_logo",
                        large_text=f"Core: {core_name} ({ext})",
                        start=activity_start_time,
                    )
            except Exception as rpc_err:
                print(f"[Discord RPC Error] {rpc_err}. Attempting to reconnect...")
                try:
                    rpc.connect()
                    print("[Discord RPC] Reconnected successfully!")
                except Exception as reconnect_err:
                    print(f"[Discord RPC] Reconnection failed: {reconnect_err}")

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print("\nShutting down MiSTer Discord RPC...")
        try:
            rpc.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
