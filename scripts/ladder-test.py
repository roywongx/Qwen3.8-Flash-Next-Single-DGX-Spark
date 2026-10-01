#!/usr/bin/env python3
"""Ladder test for the local vLLM server on DGX Spark.

Sends synthetic prompts of increasing token count and records what the server
does, so we can find the boundary where it stops being reliable.

Why a ladder and not one big request: NVIDIA's own issue #97
(NVIDIA/dgx-spark-playbooks) reports 32K and 64K completing while 131K triggers
`NV_ERR_NO_MEMORY` from `_memdescAllocInternal @ mem_desc.c:1359` and a hard
reset. The failure is memory-pressure driven, so the useful answer is the
boundary, and the only way to find it safely is to step up and stop at the
first sign of trouble.

Every level is a separate invocation, so a runaway process can be killed
between levels without taking the test harness with it.

Usage: ladder-test.py <tokens> [--max-tokens N]
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

UPSTREAM = "http://127.0.0.1:8000"
# Measured on this host with /tokenize: mixed Chinese JSON is the dense case.
TOKENS_PER_CHAR = 0.608
CONSECUTIVE_LINES = 8


def mem_snapshot():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            info[k] = int(v.split()[0]) * 1024
    nvrm = subprocess.run(
        ["journalctl", "-k", "--since", "5 min ago", "--no-pager"],
        capture_output=True, text=True).stdout.count("NV_ERR_NO_MEMORY")
    return {
        "MemFree": info["MemFree"] / 2**30,
        "MemAvailable": info["MemAvailable"] / 2**30,
        "SwapFree": info["SwapFree"] / 2**30,
        "nvrm_5min": nvrm,
    }


def build_prompt(target_tokens):
    """Dense synthetic content, so the token count is what we asked for."""
    line = ("服务健康检查：节点 n%06d 延迟 %d ms，丢包率 %.2f%%，"
            "上游返回码 %d，队列深度 %d，状态 degraded。")
    body = []
    size = 0
    while size < int(target_tokens / TOKENS_PER_CHAR):
        s = line % (size, size % 997, (size % 100) / 7.0, size % 599, size % 313)
        body.append(s)
        size += len(s)
    return "".join(body)


def alive():
    try:
        with urllib.request.urlopen(f"{UPSTREAM}/health", timeout=8) as r:
            return r.status == 200
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tokens", type=int)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--timeout", type=int, default=900)
    args = ap.parse_args()

    if not alive():
        print(json.dumps({"level": args.tokens, "ok": False,
                          "error": "server not alive before test"}))
        return 2

    before = mem_snapshot()
    prompt = build_prompt(args.tokens)

    # Ask the server what it thinks the prompt weighs, so we report the real
    # number instead of our estimate.
    req = urllib.request.Request(
        f"{UPSTREAM}/tokenize",
        data=json.dumps({"model": "local-main", "prompt": prompt}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            real_tokens = json.load(r).get("count")
    except Exception as e:
        print(json.dumps({"level": args.tokens, "ok": False,
                          "error": f"tokenize failed: {e}"}))
        return 2

    t0 = time.time()
    payload = {
        "model": "local-main",
        "max_tokens": args.max_tokens,
        "messages": [{"role": "user",
                      "content": prompt + "\n\n只回复 OK 两个字符。"}],
    }
    req = urllib.request.Request(
        f"{UPSTREAM}/v1/messages",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    result = {"level": args.tokens, "real_tokens": real_tokens,
              "mem_before": {k: round(v, 2) for k, v in before.items()}}
    try:
        with urllib.request.urlopen(req, timeout=args.timeout) as r:
            body = json.load(r)
        result["ok"] = True
        result["http"] = r.status
        result["usage"] = body.get("usage")
        result["stop_reason"] = body.get("stop_reason")
    except urllib.error.HTTPError as e:
        result["ok"] = False
        result["http"] = e.code
        result["error"] = e.read()[:400].decode("utf-8", "replace")
    except Exception as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"

    result["elapsed_s"] = round(time.time() - t0, 1)
    time.sleep(3)
    result["mem_after"] = {k: round(v, 2) for k, v in mem_snapshot().items()}
    result["alive_after"] = alive()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
