#!/usr/bin/env python3
"""PreToolUse hook: クレジット課金されるMCPツール(vidIQ / Higgsfield)の無駄打ちを機械的に止める。

CLAUDE.md のガードレール 1〜3 を強制する:
  1. 同一ツール・同一引数は 10 分以内に 2 回まで(初回 + リトライ 1 回)
  2. 重い呼び出し(HEAVY_TOOLS)の前に、同じサーバーの残高確認が 10 分以内に必要
  3. 同一ツールは引数違いでも 60 秒以内に 3 回まで

ブロック時は exit 2 + stderr で理由を Claude に返す。状態は .claude/state/ に保存(git管理外)。
"""
import fcntl
import hashlib
import json
import os
import sys
import time

SAME_ARGS_WINDOW = 600   # 秒: 同一引数の再試行を数える期間
SAME_ARGS_MAX = 2        # 初回 + リトライ1回
BURST_WINDOW = 60        # 秒: 同一ツールの連打を数える期間
BURST_MAX = 3
BALANCE_FRESH = 600      # 秒: 残高確認の有効期限

# 無料かつポーリング用途で連続呼び出しが正当なもの(制限対象外)
FREE_POLLING = {
    "vidiq_job_poll",
    "vidiq_jobs_list",
    "vidiq_instagram_publish_reel",  # 継続呼び出しは無料・5秒間隔で叩く仕様
    "vidiq_instagram_connected_accounts",
    "vidiq_instagram_owner_insights",
    "vidiq_bookmarks_list",
}

# 事前の残高確認を必須にする重い呼び出し(15クレジット以上 or *_watch 系)。
# コストが判明したものは随時ここに追加する。
HEAVY_TOOLS = {
    "vidiq_video_watch",
    "vidiq_watch_shortform_content",
    "vidiq_generate_video",
    "vidiq_generate_broll",
    "vidiq_generate_clips",
    "vidiq_motion_graphics",
}


def is_balance_tool(name):
    return "balance" in name.lower()


def is_heavy(name):
    return name in HEAVY_TOOLS or "watch" in name.lower()


def block(msg):
    sys.stderr.write(
        f"[mcp_credit_guard] {msg}\n"
        "自動で再試行せず、原因(引数・残高・サービス状態)を確認してユーザーに報告すること。\n"
    )
    sys.exit(2)


def main():
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return  # 入力が読めない場合は判断せず通す

    full_name = payload.get("tool_name", "")
    parts = full_name.split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp":
        return
    server, tool = parts[1], parts[2]

    args = json.dumps(payload.get("tool_input", {}), sort_keys=True, ensure_ascii=False)
    args_hash = hashlib.sha256(f"{full_name}|{args}".encode()).hexdigest()[:16]

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    state_dir = os.path.join(project_dir, ".claude", "state")
    os.makedirs(state_dir, exist_ok=True)
    state_path = os.path.join(state_dir, "mcp_credit_guard.json")

    now = time.time()
    with open(os.path.join(state_dir, "mcp_credit_guard.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # 並列のツール呼び出しで状態が競合しないように

        try:
            with open(state_path) as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        calls = [c for c in state.get("calls", []) if now - c["t"] < SAME_ARGS_WINDOW]
        balance_at = state.get("balance_at", {})

        def save():
            state["calls"] = calls
            state["balance_at"] = balance_at
            with open(state_path, "w") as f:
                json.dump(state, f)

        if is_balance_tool(tool):
            balance_at[server] = now
            save()
            return
        if tool in FREE_POLLING:
            return

        same_args = sum(1 for c in calls if c["h"] == args_hash)
        if same_args >= SAME_ARGS_MAX:
            block(
                f"{full_name} を同一引数で直近{SAME_ARGS_WINDOW // 60}分に{same_args}回呼んでいます"
                f"(上限: 初回+リトライ1回)。"
            )

        burst = sum(1 for c in calls if c["n"] == full_name and now - c["t"] < BURST_WINDOW)
        if burst >= BURST_MAX:
            block(f"{full_name} を直近{BURST_WINDOW}秒に{burst}回呼んでいます(上限{BURST_MAX}回)。")

        if is_heavy(tool) and now - balance_at.get(server, 0) > BALANCE_FRESH:
            block(
                f"{full_name} は重い呼び出しです。先に {server} の残高確認ツールを呼び、"
                "残高がコスト以上あることを確かめてください。"
            )

        calls.append({"t": now, "n": full_name, "h": args_hash})
        save()


if __name__ == "__main__":
    main()
