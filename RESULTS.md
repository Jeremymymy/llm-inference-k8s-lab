# 實驗數據紀錄

> 邊做邊填。`README.md` 的摘要表從這裡來。
> **量測規則**：每組先熱身 1 次（丟掉），再正式跑 1 次 —— **數字都是單次量測，不是中位數**，差距小於約 5% 不下結論；一次只改一個變數；正確性與效能必須成對記錄。

---

## 基準線（不是我量的，是對照用的）

來源：Ed Donner 課程 `week7/results.ipynb` 的 leaderboard，同一個模型、同一份測試集。

| 方法 | MAE |
|---|---|
| 固定猜平均值 | $106.18 |
| 人類（課程作者） | $87.62 |
| XGBoost | $68.23 |
| 未微調的 Llama 3.2（4-bit） | $110.72（課程 evaluator；本 repo 的 `eval/measure.py` 實測 base = **$200.29**，見正確性 #5，兩者不可直接比） |
| 深度神經網路 | $46.49 |
| GPT-5.1 | $44.74 |
| **微調後（要復現的目標）** | **$39.85** |

---

## 正確性

測試集 `ed-donner/items_prompts_full` 的 test split，**size=200**（改了就不可比）。

| # | 日期 | 環境 | 權重精度 | LoRA 方式 | MAE | R² | Hit rate | 備註 |
|---|---|---|---|---|---|---|---|---|
| 1 | 2026-09-25 | Mac / llama.cpp | Q8_0 | GGUF f16 runtime | $40.79 | 72.1% | 75.0% | Phase 0；4 次 MAE 完全相同 |
| 2 | 2026-10-18 | GPU / vLLM | bf16 | runtime, rank 256 | $40.74 | 71.9% | 75.0% | Phase 1 主線；`--max-loras 2`（多掛 `pricer2`） |
| 3 | — | GPU / vLLM | bf16 | runtime, **預設 rank 16** | 起不來 | — | — | 大聲失敗；精簡路線未實跑，依 vLLM 原始碼推論 |
| 4 | 2026-10-18 | GPU / vLLM | bf16 | merged | $40.80 | 72.0% | 74.0% | 與 #2 一致 ✅ |
| 5 | 2026-10-18 | GPU / vLLM | bf16 | 無（base） | $200.29 | -695.9% | 48.5% | **不是** leaderboard 的 $110；解析正確，base 常猜 `1,999.99` 這類價格，誤差最大 8 筆占總誤差 30% |
| 6 | 2026-10-18 | GPU / vLLM | **FP8** | merged | $40.15 | 73.4% | 75.5% | 量化代價（跟 #4 比） |
| 7 | 2026-10-20 | k3s / vLLM | bf16 | runtime, rank 256 | $40.79 | 71.9% | 75.0% | 與 #2 一致（差 $0.05）；同設定再量一次 $40.78、time-sliced 2 pod $40.53 —— vLLM 併發下批次組合每次不同，bf16 有微小雜訊，不是逐位元可重現 |

**#2 vs #5 的差距** = 請求打到 base（無聲失敗）的代價：$159.55（MAE 變成約 5 倍）—— 在這套評估裡，「LoRA 沒生效」的指紋是 **約 $200**
**#2 vs #1 的差距** = Q8_0 量化的代價：−$0.05（0.1%，單次量測不下結論）—— Q8_0 對這個任務幾乎無損
**#4 vs #6 的差距** = FP8 量化的代價（兩者都是 merged，只差量化）：−$0.65（FP8 反而略好 1.6%，<5% 不下結論）—— 沒有可量到的代價

---

## Phase 0.5（kind 三節點）實測訊息

2026-10-16～17，kind v1.37.0（1 control-plane + 2 worker），OrbStack VM 12 GiB，精簡路線。以下都是實際終端輸出（節錄），不是推論。Part 6、8 使用者先做過，這裡的輸出是 2026-10-17 在同一個叢集上重跑取得的。

| Part | 觀察到的 | 實際輸出 / 數字 | 跟 docs 預期的差異 |
|---|---|---|---|
| 2. RWO 跨節點 | 新 pod 卡 `Pending` | `0/3 nodes are available: 1 Insufficient memory, 1 node(s) didn't match PersistentVolume's node affinity, 1 node(s) had untolerated taint(s).` | worker 那行是 `Insufficient memory` 而非 anti-affinity（scheduler 每節點只報第一個失敗的檢查）；patch 同時改 replicas + template → 舊 RS 先擴到 2（同擠 worker）+ 新 RS 1 個 Pending，共 3 個 pod。docs 已補說明 |
| 5. spread | 兩個 pricer-api 都在 worker2 | — | 5.1 的 `pricer.io/gpu` taint 還在，pricer-api 只剩 worker2 可去，`ScheduleAnyway` 擠一起。docs 已補說明 |
| 6. drain + PDB | drain 卡住直到 timeout | `error when evicting pods/"model-server-…" -n "pricer" (will retry after 5s): Cannot evict pod as it would violate the pod's disruption budget.`（`ALLOWED DISRUPTIONS 0`，`--timeout=30s` 後 `global timeout reached`） | 符合 |
| 7. ResourceQuota | `DESIRED 5 / CURRENT 3`，pod 建不出來 | `exceeded quota: pricer-quota, requested: limits.memory=7Gi,requests.cpu=2,requests.memory=5Gi, used: limits.memory=22016Mi,requests.cpu=6100m,requests.memory=15616Mi, limited: limits.memory=24Gi,requests.cpu=8,requests.memory=16Gi` | 符合；三項同時超過。used 含 2 個 Pending 的 model-server（Pending 也算配額） |
| 8. 假 GPU | gpu-hog（要 4 張，剩 3 張）`Pending` | `0/3 nodes are available: 1 node(s) had untolerated taint(s), 2 Insufficient example.com/gpu.` | 符合 |
| 9. 搶佔（第一次，舊順序） | 被踢的是**舊 model-server** 而非 batch | `pod/model-server-759d676bff-tttm5 Preempted by pod f3db53a0-… on node pricer-lab-worker`；3 個 batch pod 在 12 秒後 `exit 22` | 舊 pod 建立時沒有 priorityClassName（priority 0 < batch 100），被選為 victim；搶佔不走 rollout，造成停機。docs 9.1 已改順序並寫進 ⚠️ 陷阱 |
| 9. 搶佔（第二次，新順序） | batch 被踢，補的 batch pod 不反搶 | `pod/batch-eval-9pdfc Preempted by pod 3a012699-…`；`preemption: not eligible due to preemptionPolicy=Never.` | 符合 |
| 9/10. Job | 3 個平行、陸續 `Completed` | `COMPLETIONS 60 / PARALLEL 3`，觀察時 `SUCCEEDED 8 / ACTIVE 3 / FAILED 1`（FAILED 是被搶佔的那個） | 符合 |

---

## 本機 HPA 壓測（Phase 0）

Mac / OrbStack（VM 8 GiB）/ llama.cpp Q8_0 + LoRA，`--parallel 4`。`eval/measure.py --size 200 --workers 30`，打 model-server Service（docs/02 9.4）。**各跑一次，沒有熱身**：這裡驗證的是 HPA 機制，不是效能數字。

| label | MAE | R² | Hit rate | p50 (ms) | p95 (ms) | req/s | 備註 |
|---|---|---|---|---|---|---|---|
| hpa-load-test | $41.49 | 71.5% | 74.5% | 30123 | 59212 | 0.8 | 2026-09-27；prompt cache 開著（預設 8 GiB），model-server 4.3→6.5 GiB，VM System OOM 砍 grafana；p95 可能被記憶體壓力拖慢 |
| hpa-load-test-cache-ram-0 | $40.86 | 71.9% | 75.0% | 31913 | 37582 | 0.9 | 2026-09-27；`--cache-ram 0`，高峰 4.66 GiB、無 OOM。兩次都有 `SuccessfulRescale` → 2，第二個 pod `Insufficient memory` Pending |

---

## 效能

2026-10-18，g6.xlarge / NVIDIA L4（23034 MiB，compute 8.9）。**vLLM 0.31.0**（`vllm/vllm-openai@sha256:c1c9f6fd5c109ba7f0546a59f5b2f15fb87f64c77782e90a27b648b42a8e67c3`），`--gpu-memory-utilization 0.85`。工具：`vllm bench serve`，`--random-input-len 150 --random-output-len 5`，飽和 = `--num-prompts 500 --request-rate inf`。**每次壓測都用沒用過的 `--seed`**（理由見下一節）。全部是單次量測。

### ⚠️ 量測陷阱：prefix caching 讓同一批 prompt 快 7 倍

vLLM V1 預設開 prefix caching，`vllm bench serve` 的 random dataset 預設 `seed=0` —— 不指定 seed 的話，每次產生的 prompt 都一樣，第二次之後 prefill 幾乎全部命中快取。

| 同一個 seed=42 連跑兩次 | 新增 prefix cache 查詢 | 新增命中 | 命中率 | 飽和吞吐 (req/s) | p99 TTFT |
|---|---|---|---|---|---|
| 第 1 次 | 75,000 | 720 | 1% | 35.89 | 13,666 ms |
| 第 2 次 | 75,000 | 72,000 | 96% | 258.34 | 1,633 ms |

一開始照 runbook（沒指定 seed）量到的「253 req/s」、以及 300 筆固定 rate 掃描「到 280 req/s 都沒有轉折」，全部是這個假象，已作廢重量。計數器：`curl -s localhost:8000/metrics | grep -E "^vllm:prefix_cache_(queries|hits)_total"`。

### `--max-model-len` 的影響（預期：131072 起不來，其餘兩組吞吐差不多 —— KV cache 按需分配）✅ 符合

| `--max-model-len` | KV cache size (tokens) | Maximum concurrency | 飽和吞吐 (req/s) | p50 TTFT | p95 TTFT | p99 TTFT |
|---|---|---|---|---|---|---|
| 131072（預設） | 起不來：`ValueError: ... (14.0 GiB KV cache is needed, which is larger than the available KV cache memory (9.07 GiB). Based on the available memory, the estimated maximum model length is 84864.` | — | — | — | — | — |
| 32768 | 84,880 | 2.59x | 35.92 | 7,230 ms | 13,282 ms | 13,644 ms |
| 512 | 84,864 | 165.75x | 35.77 | 7,236 ms | 13,341 ms | 13,708 ms |

estimated maximum model length（84,864）= 512 那組的 KV cache size：KV 總量跟 `--max-model-len` 無關，它只決定啟動檢查與 `Maximum concurrency` 的分母。

### `--max-num-batched-tokens` 的影響（其餘同 512 那組）

| `--max-num-batched-tokens` | 飽和吞吐 (req/s) | 飽和 p50 TPOT | 固定 rate 30 req/s 下的 p99 TTFT | 備註 |
|---|---|---|---|---|
| 預設（log 不印；讀 0.31 `vllm/engine/arg_utils.py`：<70 GiB 的 GPU、API server = **2048**） | 35.77 | 356 ms | 495 ms | log 只有 `chunked_prefill=True` |
| 8192 | 35.90 | **994 ms** | 530 ms（+7%，單次） | KV cache 84,608（少 256） |

**結論**：吞吐不變 → 瓶頸不在批次大小（compute-bound）。代價是 decode 被更大的 prefill 批次卡住，TPOT 變 2.8 倍；這個 workload 只輸出 5 token 所以看不太出來，長輸出會很明顯。

### request-rate 掃描（找 SLA 轉折點）

runtime LoRA（512 那組）。每個 rate 跑約 15 秒（`--num-prompts = rate × 15`），seed = 1000 + rate。

| req/s（設定） | 實際 req/s | p50 TTFT | p95 TTFT | **p99 TTFT** |
|---|---|---|---|---|
| 10 | 9.85 | 118 ms | 180 ms | 196 ms |
| 20 | 19.63 | 158 ms | 246 ms | 331 ms |
| 25 | 24.49 | 201 ms | 323 ms | 366 ms |
| 30 | 29.32 | 267 ms | 440 ms | 495 ms |
| 35 | 34.03 | 505 ms | 828 ms | 910 ms |
| 40 | 35.71 | 1,345 ms | 1,866 ms | 1,956 ms |
| 50 | 36.42 | 2,652 ms | 5,230 ms | 5,555 ms |

**p99 開始急遽上翹的點**：約 **30 req/s**（飽和吞吐的 84%）→ 這是實際容量上限，也是 HPA 門檻的依據。超過約 36 req/s 實際吞吐就上不去，佇列隨時間線性變長。

固定 300 筆的掃描在高 rate 只跑 1–2 秒，佇列來不及排起來，量不到轉折 —— 所以改成固定時長。

### 瓶頸判讀（docs/05 Part 7，512 那組飽和時的尖峰）

| GPU `sm`（`nvidia-smi dmon`） | `kv_cache_usage_perc` | `num_requests_waiting` | CPU（全機 us / vLLM 程序） | 診斷 |
|---|---|---|---|---|
| 100% | 0.128 | 430 | 28% / 109% | **compute-bound**（符合 docs/05 7.3 預測） |

### 量化對照（品質 vs 吞吐）

都是 merged、`--max-model-len 512`。

| 量化 | 權重（`Model loading took`） | KV cache size | MAE | 飽和吞吐 | p95 TTFT | p50 TPOT |
|---|---|---|---|---|---|---|
| bf16 | 6.04 GiB | 106,400 | $40.80 | 53.38 | 8,966 ms | 238 ms |
| FP8（`--quantization fp8`） | 3.57 GiB | 127,520 | $40.15 | **73.28（+37%）** | 6,512 ms | 169 ms |
| AWQ（選作） | 未做 | | | | | |

FP8 在 L4 上選到 `CutlassFP8ScaledMMLinearKernel`（真 W8A8，不是只存 FP8 權重），量化了 112 層（28 層 × qkv/o/gate_up/down）。所以 compute-bound 的 prefill 也變快 —— 跟 docs/05 5.3 原本「量化不一定變快」的預測相反。0.31 會警告 `--quantization fp8 is deprecated ... use --quantization fp8_per_tensor`。

### runtime LoRA vs merged（原創實驗，r=256）

> runtime 那列是用 `--max-loras 2`（多掛 `pricer2`）量的，比 merged 多一個變因（docs/04 Part 6.3）。

| 方式 | MAE | 權重（`Model loading took`） | KV cache size | 飽和吞吐 | p95 TTFT | 可熱插拔 |
|---|---|---|---|---|---|---|
| runtime LoRA | $40.74 | 7.76 GiB | 84,864 | 35.77 | 13,341 ms | ✅ |
| merged | $40.80 | 6.04 GiB | 106,400 | **53.38（+49%）** | 8,966 ms | ❌ |

LoRA 預留約 1.7 GiB 顯存（rank 256 × 2 個 slot），KV 少 20%；吞吐差距大到不是 5% 雜訊，但有一部分可能來自 `--max-loras 2`。merge 本身（CPU、在 vLLM image 裡）花 1 分 48 秒，產出 6.0 GB 的 `model.safetensors`。

### 多 LoRA 同時服務

`/v1/models` 列出 `['base', 'pricer', 'pricer2']`，`pricer` 與 `pricer2` 同一個 prompt 都回 `'99.00'`。1 vs 2 個 adapter 的效能差異精簡路線跳過。

| 情境 | 吞吐 | p95 TTFT |
|---|---|---|
| 1 個 adapter | 未做 | |
| 2 個 adapter（流量各半） | 未做 | |

---

## GPU time-slicing 的代價

| 設定 | 吞吐 (req/s) | p50 TTFT | **p99 TTFT** | MAE | 顯存/pod |
|---|---|---|---|---|---|
2026-10-20，k3s、vLLM 0.31.0、runtime LoRA r=256，bench pod 打 Service，`--request-rate inf`、500 筆，熱身一次後的正式那次。

| 設定 | 吞吐 (req/s) | p50 TTFT | **p99 TTFT** | MAE | 顯存/pod |
|---|---|---|---|---|---|
| 1 pod 獨佔（0.85） | 34.54 | 7,532 ms | 14,198 ms | $40.78 | 18,780 MiB；KV 93,952 tokens（並發上限 183） |
| 1 pod（0.42，對照組） | 30.86 | 8,268 ms | 15,930 ms | — | 9,068 MiB；KV 5,232 tokens（並發上限 10.2） |
| 2 pod time-sliced（各 0.42） | 28.13 | 8,957 ms | 17,442 ms | $40.53 | 9,068 MiB × 2；KV 各 5,232 tokens |
| 3 pod time-sliced（選作：bf16 放不下，需 FP8 merged） | 未做 | | | | |

`Model loading took` 兩種設定都是 6.9 GiB（權重 + 1 個 r=256 LoRA 槽位），跟 docs/06 Part 7.2 的推算一致；但 KV 實測 5,232 tokens，推算的約 2,000 太悲觀。

**結論**：2 pod 合計吞吐比 1 pod 獨佔**少 19%**，p99 TTFT 多 23%。加了對照組才拆得開：顯存切成 0.42、KV 只剩約 10 個請求的並發，就先掉 10.7%（34.54 → 30.86）；兩個 pod 輪流用 GPU 再掉 8.8%（30.86 → 28.13）。這個 compute-bound 的 workload 上，time-slicing 換不到吞吐，換到的是「同時跑兩份」與「rollout 不必停機」；代價約一半來自每個 pod 分到的顯存變小，一半來自切換本身。MAE 不受影響。

---

## GPU 真實利用率（DCGM）

壓測飽和時同時量測：

| 指標 | 數值 | 意義 |
|---|---|---|
| `DCGM_FI_DEV_GPU_UTIL` | 99% | 「有 kernel 在跑」—— **會騙人** |
| `DCGM_FI_PROF_SM_ACTIVE` | 0.95 | SM 上至少有一個 warp 的時間比例 |
| `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` | 0.52 | Tensor Core 真正在做矩陣乘法的時間比例 |

條件：2026-10-20，1 pod（0.42，time-slicing 開著但只有 1 個 pod），2000 筆、`--request-rate inf`、seed 8，30.99 req/s。exporter 約 **30 秒**更新一次，`PROF_*` 是那 30 秒的平均；取的是整段都落在壓測期間的那一筆（05:10:59 → 05:11:30）。原始取樣在 `bench-raw/8-dcgm.txt`。

**GPU_UTIL 與 SM_ACTIVE 的落差**：幾乎沒有（99% vs 95%）—— 推翻 docs/06 Part 8.3「SM_ACTIVE 明顯更低」的預期。落差在下一層：Tensor Core 只有 52% 的時間在算。
**這代表**：SM 都排滿了工作（compute-bound，跟 Phase 2 的瓶頸判讀一致），但最貴的 Tensor Core 只用了一半，其餘時間在跑 attention、逐元素運算、取樣與資料搬移。「GPU_UTIL 100% = 卡滿了」只對一半；要提高產出該往「讓更多時間跑在 Tensor Core 上」想 —— FP8 在 L4 上 prefill +37%（量化對照）就是這個方向。

---

## 關鍵時間數字

| 項目 | 數值 | 用在哪 |
|---|---|---|
| llama.cpp 冷啟動（Mac，CPU） | 約 70 秒（權重已在 PVC；2026-09-27 rollout 實測，建立→ready） | Phase 0 的 `startupProbe` |
| vLLM 冷啟動（含權重下載） | 249 秒（runtime LoRA；image 已先 pull，`~/vllm-cache` 是空的；`Model loading took` 61 秒） | |
| vLLM 冷啟動（權重已快取） | 121 秒（runtime LoRA 與 merged bf16 都是；`Model loading` 6–35 秒，其餘是編譯與 CUDA graph capture） | **Phase 3 的 `startupProbe` 與 HPA 穩定視窗** |
| k8s 冷啟動（含下載權重進 PVC） | 230 秒（image 已先 `crictl pull`；pod 建立 → Ready；`Model loading` 61 秒） | k3s 的 `startupProbe`（給 900 秒） |
| k8s 重啟（權重已在 PVC、沒有編譯快取） | 201 秒（比 docker 的 121 秒多：k8s 裡沒有 `~/vllm-cache`） | |
| 單卡滾動更新的停機時間 | **191 秒**（選項 A：`maxSurge 0 / maxUnavailable 1`；bench pod 每秒打 Service，第一個 DOWN → 第一個 200） | 「該不該多買一張卡」的決策依據 |

---

## 成本紀錄

| 日期 | 階段 | 機器時數 | 花費 | 累計 |
|---|---|---|---|---|
| 2026-10-20 | Phase 3（g6.xlarge，$0.8048/hr） | 約 3.5 | 約 $2.8（不含 EBS） | |

**預算上限：$80（月預算警示）**
