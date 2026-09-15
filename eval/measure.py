#!/usr/bin/env python3
"""
最小評估工具 —— 對任何 OpenAI 相容的 /v1/completions 端點跑估價測試集。

設計原則：
  1. 指標與課程 week7/results.ipynb 的 leaderboard 可比（MAE 為主指標），
     這樣你的數字才能跟 $39.85 那個門檻對照。
  2. 只依賴 datasets + 標準函式庫。不 import 課程 repo 的任何程式碼。
  3. 可以打任何後端：llama.cpp server、vLLM、甚至 Modal 的 proxy。
     介面一樣，數字就可比。

用法：
    uv run --with datasets eval/measure.py \
        --base-url http://localhost:8000 \
        --model pricer \
        --size 200 \
        --label "vllm-bf16-lora256"

輸出會印成一行 Markdown 表格列，直接貼進 RESULTS.md。
"""

import argparse
import concurrent.futures
import json
import re
import statistics
import time
import urllib.error
import urllib.request

PRICE_RE = re.compile(r"[-+]?\d*\.\d+|\d+")


def parse_price(text: str) -> float:
    """從模型輸出抽出價格。

    注意：vLLM 的 /v1/completions 只回「續寫」部分，不含 prompt。
    所以這裡直接對回傳文字做 regex，不要照抄 Modal 版的
    `result.split("Price is $")[1]` —— 那會 IndexError。
    """
    cleaned = text.replace("$", "").replace(",", "")
    match = PRICE_RE.search(cleaned)
    return float(match.group()) if match else 0.0


def predict(base_url, model, prompt, max_tokens, temperature, timeout):
    """打一次 /v1/completions，回 (預測價格, 延遲秒數, 錯誤訊息)。"""
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return None, time.perf_counter() - start, str(exc)
    elapsed = time.perf_counter() - start
    return parse_price(body["choices"][0]["text"]), elapsed, None


def hit_colour(error: float, truth: float) -> str:
    """與課程 evaluator 相同的分級規則，讓 hit rate 可比。"""
    if error < 40 or (truth and error / truth < 0.2):
        return "green"
    if error < 80 or (truth and error / truth < 0.4):
        return "orange"
    return "red"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True, help="例如 http://localhost:8000")
    ap.add_argument("--model", required=True,
                    help="vLLM 用 LoRA 時填 adapter 名；否則填模型名")
    ap.add_argument("--dataset", default="ed-donner/items_prompts_full")
    ap.add_argument("--split", default="test")
    ap.add_argument("--size", type=int, default=200,
                    help="測幾筆。課程慣例是 200，改了就不可比")
    ap.add_argument("--workers", type=int, default=5,
                    help="並行數。量正確性用 5；想順便看並發行為再調高")
    ap.add_argument("--max-tokens", type=int, default=5)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--label", default="unnamed", help="這次跑的名字，會印在輸出裡")
    args = ap.parse_args()

    from datasets import load_dataset

    print(f"載入 {args.dataset} [{args.split}] …")
    data = load_dataset(args.dataset, split=args.split).select(range(args.size))

    def run_one(row):
        guess, latency, err = predict(
            args.base_url, args.model, row["prompt"],
            args.max_tokens, args.temperature, args.timeout,
        )
        return guess, float(row["completion"]), latency, err

    wall_start = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run_one, data))
    wall = time.perf_counter() - wall_start

    failures = [r for r in results if r[3] is not None]
    ok = [r for r in results if r[3] is None]
    if not ok:
        print("全部失敗。第一個錯誤：", failures[0][3])
        raise SystemExit(1)

    errors = [abs(g - t) for g, t, _, _ in ok]
    truths = [t for _, t, _, _ in ok]
    guesses = [g for g, _, _, _ in ok]
    latencies = sorted(lat for _, _, lat, _ in ok)

    mae = statistics.fmean(errors)
    rmse = statistics.fmean(e * e for e in errors) ** 0.5
    mean_truth = statistics.fmean(truths)
    ss_res = sum((g - t) ** 2 for g, t in zip(guesses, truths))
    ss_tot = sum((t - mean_truth) ** 2 for t in truths)
    r2 = (1 - ss_res / ss_tot) * 100 if ss_tot else float("nan")
    hits = sum(1 for e, t in zip(errors, truths) if hit_colour(e, t) == "green")

    def pct(p):
        return latencies[min(int(len(latencies) * p), len(latencies) - 1)]

    print()
    print(f"=== {args.label} ===")
    print(f"成功 {len(ok)}/{args.size}，失敗 {len(failures)}")
    print(f"MAE      ${mae:,.2f}      <- 主指標，跟 leaderboard 比這個")
    print(f"RMSE     ${rmse:,.2f}")
    print(f"R²       {r2:.1f}%")
    print(f"Hit rate {hits / len(ok) * 100:.1f}%  (誤差<$40 或 <20%)")
    print(f"延遲     p50 {pct(0.5)*1000:.0f}ms / p95 {pct(0.95)*1000:.0f}ms "
          f"/ p99 {pct(0.99)*1000:.0f}ms")
    print(f"總耗時   {wall:.1f}s  ({len(ok)/wall:.1f} req/s @ workers={args.workers})")
    if failures:
        print(f"第一個錯誤：{failures[0][3]}")
    print()
    print("貼進 RESULTS.md：")
    print(f"| {args.label} | ${mae:,.2f} | {r2:.1f}% | {hits/len(ok)*100:.1f}% | "
          f"{pct(0.5)*1000:.0f} | {pct(0.95)*1000:.0f} | {len(ok)/wall:.1f} |")


if __name__ == "__main__":
    main()
