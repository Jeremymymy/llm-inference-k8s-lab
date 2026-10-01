# 實驗數據紀錄

> 邊做邊填。`README.md` 的摘要表從這裡來。
> **量測規則**：每組跑 3 次取中位數、第一次丟掉（warmup）；一次只改一個變數；正確性與效能必須成對記錄。

---

## 基準線（不是我量的，是對照用的）

來源：Ed Donner 課程 `week7/results.ipynb` 的 leaderboard，同一個模型、同一份測試集。

| 方法 | MAE |
|---|---|
| 固定猜平均值 | $106.18 |
| 人類（課程作者） | $87.62 |
| XGBoost | $68.23 |
| 未微調的 Llama 3.2（4-bit） | **$110.72** ← 「LoRA 沒生效」的指紋 |
| 深度神經網路 | $46.49 |
| GPT-5.1 | $44.74 |
| **微調後（要復現的目標）** | **$39.85** |

---

## 正確性

測試集 `ed-donner/items_prompts_full` 的 test split，**size=200**（改了就不可比）。

| # | 日期 | 環境 | 權重精度 | LoRA 方式 | MAE | R² | Hit rate | 備註 |
|---|---|---|---|---|---|---|---|---|
| 1 | 2026-09-25 | Mac / llama.cpp | Q8_0 | GGUF f16 runtime | $40.79 | 72.1% | 75.0% | Phase 0；4 次 MAE 完全相同 |
| 2 | | GPU / vLLM | bf16 | runtime, rank 256 | | | | Phase 1 主線 |
| 3 | | GPU / vLLM | bf16 | runtime, **預設 rank 16** | 起不來 | — | — | 大聲失敗：備註欄貼啟動錯誤訊息 |
| 4 | | GPU / vLLM | bf16 | merged | | | | 應與 #2 一致 |
| 5 | | GPU / vLLM | bf16 | 無（base） | | | | 應接近 $110 |
| 6 | | GPU / vLLM | **FP8** | merged | | | | 量化代價（跟 #4 比） |
| 7 | | k3s / vLLM | bf16 | runtime, rank 256 | | | | **應與 #2 完全一致** |

**#2 vs #5 的差距** = 請求打到 base（無聲失敗）的代價：___
**#2 vs #1 的差距** = Q8_0 量化的代價：___
**#4 vs #6 的差距** = FP8 量化的代價（兩者都是 merged，只差量化）：___

---

## 本機 HPA 壓測（Phase 0）

Mac / OrbStack（VM 8 GiB）/ llama.cpp Q8_0 + LoRA，`--parallel 4`。`eval/measure.py --size 200 --workers 30`，打 model-server Service（docs/02 9.4）。**各跑一次，未照 3 次取中位數**：這裡驗證的是 HPA 機制，不是效能數字。

| label | MAE | R² | Hit rate | p50 (ms) | p95 (ms) | req/s | 備註 |
|---|---|---|---|---|---|---|---|
| hpa-load-test | $41.49 | 71.5% | 74.5% | 30123 | 59212 | 0.8 | 2026-09-27；prompt cache 開著（預設 8 GiB），model-server 4.3→6.5 GiB，VM System OOM 砍 grafana；p95 可能被記憶體壓力拖慢 |
| hpa-load-test-cache-ram-0 | $40.86 | 71.9% | 75.0% | 31913 | 37582 | 0.9 | 2026-09-27；`--cache-ram 0`，高峰 4.66 GiB、無 OOM。兩次都有 `SuccessfulRescale` → 2，第二個 pod `Insufficient memory` Pending |

---

## 效能

g6.xlarge / NVIDIA L4 24GB。工具：`vllm bench serve`，`--random-input-len 150 --random-output-len 5`。

### `--max-model-len` 的影響（預期：131072 起不來，其餘三組吞吐差不多 —— KV cache 按需分配）

| `--max-model-len` | KV cache size (tokens) | Maximum concurrency | 飽和吞吐 (req/s) | p50 TTFT | p95 TTFT | p99 TTFT |
|---|---|---|---|---|---|---|
| 131072（預設） | 起不來？ | | | | | |
| 32768 | | | | | | |
| 4096 | | | | | | |
| 512 | | | | | | |

### request-rate 掃描（找 SLA 轉折點）

| req/s | p50 TTFT | p95 TTFT | **p99 TTFT** | 佇列長度 |
|---|---|---|---|---|
| 5 | | | | |
| 10 | | | | |
| 20 | | | | |
| 40 | | | | |
| 80 | | | | |
| 160 | | | | |

**p99 開始急遽上翹的點**：___ req/s → 這是實際容量上限，也是 HPA 門檻的依據。

### 量化對照（品質 vs 吞吐）

| 量化 | 顯存佔用 | MAE | 飽和吞吐 | p95 TTFT |
|---|---|---|---|---|
| bf16 | | | | |
| FP8 | | | | |
| AWQ（選作） | | | | |

### runtime LoRA vs merged（原創實驗，r=256）

| 方式 | MAE | 飽和吞吐 | p95 TTFT | 可熱插拔 |
|---|---|---|---|---|
| runtime LoRA | | | | ✅ |
| merged | | | | ❌ |

### 多 LoRA 同時服務

| 情境 | 吞吐 | p95 TTFT |
|---|---|---|
| 1 個 adapter | | |
| 2 個 adapter（流量各半） | | |

---

## GPU time-slicing 的代價

| 設定 | 吞吐 (req/s) | p50 TTFT | **p99 TTFT** | MAE | 顯存/pod |
|---|---|---|---|---|---|
| 1 pod 獨佔 | | | | | |
| 2 pod time-sliced | | | | | |
| 3 pod time-sliced（選作：bf16 放不下，需 FP8 merged） | | | | | |

**結論**：___

---

## GPU 真實利用率（DCGM）

壓測飽和時同時量測：

| 指標 | 數值 | 意義 |
|---|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | | 「有 kernel 在跑」—— **會騙人** |
| `DCGM_FI_PROF_SM_ACTIVE` | | SM 真實佔用率 |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | | Tensor Core 使用率 |

**GPU_UTIL 與 SM_ACTIVE 的落差**：___
**這代表**：___

---

## 關鍵時間數字

| 項目 | 數值 | 用在哪 |
|---|---|---|
| llama.cpp 冷啟動（Mac，CPU） | 約 70 秒（權重已在 PVC；2026-09-27 rollout 實測，建立→ready） | Phase 0 的 `startupProbe` |
| vLLM 冷啟動（含權重下載） | | |
| vLLM 冷啟動（權重已快取） | | **Phase 3 的 `startupProbe` 與 HPA 穩定視窗** |
| 單卡滾動更新的停機時間 | | 「該不該多買一張卡」的決策依據 |

---

## 成本紀錄

| 日期 | 階段 | 機器時數 | 花費 | 累計 |
|---|---|---|---|---|
| | | | | |

**預算上限：$80（月預算警示）**
