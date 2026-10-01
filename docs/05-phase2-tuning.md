# LLM Inference on Kubernetes 實驗室 — 05：Phase 2，vLLM 效能調校與壓測

> 成本：**約 $12**（同一台 g6.xlarge，再累積開機 15 小時）。
> 產出：一張「設定 × 正確性 × 效能」的完整對照表，以及「瓶頸在哪」的實證。
> 這一階段是面試被追問時的分水嶺：**會部署的人很多，能說出瓶頸在哪的人很少。**

---

## 這一階段的目的

Phase 1 結束時你有一個「會動而且答案正確」的服務。Phase 2 要回答的是：

1. 它現在的極限在哪？
2. 哪個參數動了會有效？為什麼？
3. 它是 compute-bound 還是 memory-bound？**證據是什麼？**

**七個部分：**

1. 指標與量測方法論
2. `vllm bench serve`
3. `--max-model-len`：驗證一個常見的直覺
4. 其餘調校旋鈕
5. 量化對照矩陣
6. 多 LoRA 熱插拔
7. 瓶頸判讀

---

## Part 1：指標與量測方法論

### 1.1 量什麼

| 指標 | 意義 | 本 workload 的重要性 |
|---|---|---|
| **TTFT** | 第一個 token 的延遲 | ⭐⭐⭐ 輸出只有 5 token，TTFT 幾乎就是全部 |
| **TPOT / ITL** | 後續 token 的間隔 | ⭐ 只有 4 個間隔，影響很小 |
| **E2E p50/p95/p99** | 端到端延遲分布 | ⭐⭐⭐ p99 才是使用者會遇到的最壞情況 |
| **Throughput** | req/s 與 output tokens/s | ⭐⭐⭐ 決定單位成本 |
| **Goodput** | 滿足 SLA 的 req/s | ⭐⭐ 比 throughput 誠實 |

**本 workload 的特殊性必須寫進你的結論**：輸入約 150 token、輸出只有 5 token，這是一個 **prefill 主導**的 workload。含義：

- 它偏向 **compute-bound**，而不是一般 chat 場景的 memory-bound
- **量化帶來的加速會比一般情況小很多**（量化省的是記憶體頻寬）
- KV cache 需求極小，所以並發可以開得很大

**面試時這點很好用**：大部分人講的 LLM 推論常識是以 chat 為前提的。你能指出「我的 workload 是 prefill-heavy，所以那些結論不成立」，證明你是真的量過。

### 1.2 怎麼量才算數

**四條規則，違反任何一條數字就不可比：**

1. **每組先熱身 1 次（丟掉），再正式跑 1 次**（熱身是為了 CUDA graph warmup、cache 冷）。⚠️ 這是時間考量下的放寬：單次量測沒有變異數，**差距小於約 5% 的不要下結論**，`RESULTS.md` 要註明是單次
2. **固定資料集切片與隨機種子**，不同組才可比
3. **同時記正確性（MAE）與效能**。只有效能沒有正確性的數字沒有意義 —— 你可以讓一個完全答錯的服務跑得飛快
4. **一次只改一個變數**。同時改 `--max-model-len` 和 `--max-num-seqs`，你就不知道是誰的功勞

### 1.3 記錄格式

每一組實驗都記成 `RESULTS.md` 裡的一列：

```
| 設定 | MAE | p50 TTFT | p95 TTFT | req/s | KV cache size (tokens) | 備註 |
```

**`KV cache size` 那一欄特別重要** —— 它是 vLLM 啟動 log 裡印的 KV cache 容量（`docker logs vllm 2>&1 | grep -E "KV cache size|Maximum concurrency"`），代表這張卡實際能同時容納多少 token。調校時它比任何東西都誠實。

---

## Part 2：`vllm bench serve`

### 2.1 做法

```bash
# 在 GPU 機器上：直接用正在跑的 vllm 容器裡的 vllm，不用在主機上另外裝
docker exec vllm vllm bench serve \
  --backend openai \
  --base-url http://localhost:8000 \
  --model pricer \
  --tokenizer meta-llama/Llama-3.2-3B \
  --dataset-name random \
  --random-input-len 150 \
  --random-output-len 5 \
  --num-prompts 500 \
  --request-rate inf
```

**為什麼用 `docker exec` 而不是 `pip install "vllm[bench]"`**：在主機上 pip 裝 vllm 會拉一整套 torch + CUDA 函式庫，好幾 GB、十幾分鐘，而且這台機器按小時計費。`vllm/vllm-openai` 這個 image 裡本來就有 `vllm` 指令，直接借用就好。容器裡的 `localhost:8000` 就是 vLLM 自己，所以 `--base-url` 不用改。

⚠️ 代價是壓測工具跟 vLLM 擠在同一台 **4 vCPU** 的機器上，會互搶 CPU。判讀飽和吞吐時要記得這件事（見 Part 7.2）。

**為什麼一定要 `--tokenizer`**：`vllm bench serve` 要用 tokenizer 來產生「剛好 150 個 token」的隨機 prompt。沒寫 `--tokenizer` 的話它會拿 `--model` 的值去 HF 找 —— 但 `pricer` 只是我們給 LoRA 取的名字，HF 上沒有這個模型，直接報錯。
- 容器是用 HF 模型起的（有掛 `$HF_HOME`、有 `HF_TOKEN`）→ `--tokenizer meta-llama/Llama-3.2-3B`
- 容器是用 merged 權重起的（Part 5 的 FP8，只掛了 `/merged`）→ `--tokenizer /merged`

**如果報缺套件**（例如 `No module named 'pandas'`），代表這版 image 沒帶 bench 需要的額外套件：`docker exec vllm pip install pandas datasets`。注意容器是 `--rm` 起的，重啟後要重裝。

`--request-rate inf` 代表「一次全部送出」，量的是**飽和吞吐**。要量固定負載下的延遲就改成具體數字：

```bash
  --request-rate 20        # 每秒 20 個請求
```

### 2.2 為什麼 `--random-input-len 150 --random-output-len 5`

**因為要跟你的真實 workload 一致。** 用預設的 ShareGPT 資料集會量到一個完全不同的形狀（長輸出、decode 主導），結論不能套用到你的服務上。

這是很常見的錯誤：拿別人的 benchmark 數字來推論自己的系統。**輸入輸出長度的比例決定了瓶頸在哪**，換了就不是同一件事。

### 2.3 兩種量法都要做

| 量法 | `--request-rate` | 回答什麼問題 |
|---|---|---|
| **飽和測試** | `inf` | 「這台機器的天花板在哪」 |
| **固定負載** | 具體數字 | 「在 X req/s 下，p99 延遲是多少」 |

**固定負載的掃描是找 SLA 的方法**：從低到高掃 request-rate，畫出「負載 vs p99」的曲線。曲線開始急遽上翹的那個點，就是你的實際容量上限 —— 而它通常**遠低於**飽和吞吐。

```bash
for rate in 5 10 20 40 80 160; do
  echo "=== request-rate=$rate ==="
  docker exec vllm vllm bench serve --backend openai --base-url http://localhost:8000 \
    --model pricer --tokenizer meta-llama/Llama-3.2-3B --dataset-name random \
    --random-input-len 150 --random-output-len 5 \
    --num-prompts 300 --request-rate $rate
done
```

**這條曲線是 Phase 3 設定 HPA 門檻的依據** —— 找出「排隊長度到多少時 p95 開始爆掉」，那就是 `averageValue` 該設的值。

---

## Part 3：`--max-model-len` —— 驗證一個常見的直覺

### 3.1 做法

**第一步：先試預設值 131072 —— 它很可能根本起不來。**

用前景模式跑（不加 `-d`），這樣錯誤訊息會直接印在終端上：

```bash
docker stop vllm 2>/dev/null
docker run --rm --name vllm --gpus all --ipc=host -p 8000:8000 \
  -v "$HF_HOME:/root/.cache/huggingface" \
  -v "$HOME/vllm-cache:/root/.cache/vllm" \
  -v /home/ubuntu/adapter:/adapter:ro \
  -e "HF_TOKEN=$HF_TOKEN" \
  vllm/vllm-openai:latest \
    --model meta-llama/Llama-3.2-3B \
    --served-model-name base \
    --enable-lora --lora-modules pricer=/adapter pricer2=/adapter \
    --max-lora-rank 256 --max-loras 2 \
    --max-model-len 131072 \
    --gpu-memory-utilization 0.85
```

預期會看到類似這樣的錯誤，然後容器直接退出（確切措辭依 vLLM 版本而異）：

```
ValueError: To serve at least one request with the models's max seq len (131072),
(14.00 GiB KV cache is needed), which is larger than the available KV cache memory (X GiB).
Based on the available memory, the estimated maximum model length is NNNNN.
Try increasing `gpu_memory_utilization` or decreasing `max_model_len` ...
```

**把 X 和 NNNNN 記進 `RESULTS.md`。** 為什麼會這樣，見 3.2 的算式。

> 如果它**起來了**（例如新版 vLLM 改了行為），那就照原計畫把 131072 當「大」那一組，第二步的 32768 直接換成 131072。

**第二步：用 Phase 1 ① 的完整指令，只把 `--max-model-len` 改成 32768**，跟 ①（512）比。精簡路線不跑 4096：兩個端點差 64 倍，夠判斷「有沒有影響」了。

```bash
docker stop vllm 2>/dev/null
docker run --rm -d --name vllm --gpus all --ipc=host -p 8000:8000 \
  -v "$HF_HOME:/root/.cache/huggingface" \
  -v "$HOME/vllm-cache:/root/.cache/vllm" \
  -v /home/ubuntu/adapter:/adapter:ro \
  -e "HF_TOKEN=$HF_TOKEN" \
  vllm/vllm-openai:latest \
    --model meta-llama/Llama-3.2-3B \
    --served-model-name base \
    --enable-lora --lora-modules pricer=/adapter pricer2=/adapter \
    --max-lora-rank 256 --max-loras 2 \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.85

# 等就緒：輪詢 /health，而不是固定 sleep（編譯時間每次不同）。
# 最多等 15 分鐘 —— 容器起不來的話會被 --rm 刪掉，沒有 timeout 會卡死。
timeout 900 bash -c 'until curl -sf localhost:8000/health >/dev/null; do sleep 5; done' \
  || echo "沒起來，改用前景模式（拿掉 -d）重跑看錯誤"
docker logs vllm 2>&1 | grep -E "KV cache size|Maximum concurrency"
# 熱身 1 次 + 正式 1 次（Part 1.2），只記第二次
for i in 1 2; do
  docker exec vllm vllm bench serve --backend openai --base-url http://localhost:8000 \
    --model pricer --tokenizer meta-llama/Llama-3.2-3B --dataset-name random \
    --random-input-len 150 --random-output-len 5 \
    --num-prompts 500 --request-rate inf
done
```

### 3.2 常見的直覺，和 vLLM 實際的做法

**常見的直覺**：vLLM 會替每個請求預留 `max-model-len` 那麼多的 KV cache。所以設 131072 的話，99% 的空間被保留給永遠不會發生的情況；調到 512，並發會暴增。

**vLLM 實際的做法**：KV cache 是**按需分配**的。啟動時把「`--gpu-memory-utilization` 劃出的記憶體 − 權重 − 其他開銷」整塊切成 block 當作池子，每個請求只拿它**真正用到的 token** 所需的 block，多生成一個 token 才多拿一點。這正是 PagedAttention 的重點（見 `00-concepts.md` 2.3）—— 不按最長可能長度預留。

> 對照 vLLM 原始碼 `vllm/v1/core/kv_cache_manager.py` 的 `allocate_slots`：只為「已計算的 token + 這一輪要算的 token」配置 block。

所以對一個 150 token 的請求，`--max-model-len` 是 512 還是 32768，它佔的 KV cache 一樣多。

**那 `--max-model-len` 實際影響什麼？**

| 影響 | 說明 |
|---|---|
| **能不能啟動** | KV cache 至少要放得下「一條最長的序列」，否則拒絕啟動（見下面的算式） |
| **`Maximum concurrency` 那行 log** | 它是「KV cache 總 token 數 ÷ max-model-len」，是**最壞情況**的估計。調小 max-model-len 這個數字會變大，但不代表實際並發變大 |
| **單一請求的長度上限** | 超過的請求直接被拒絕。對本 workload 是一道防線，不是效能旋鈕 |

> 這個「常識」很可能來自舊版 vLLM：沒開 chunked prefill 的時候，`max_num_batched_tokens` 預設至少等於 max-model-len，啟動時的記憶體 profiling 會按那麼長的一批來估 activation，於是 max-model-len 越大、留給 KV cache 的越少。現在 chunked prefill 預設開啟、`max_num_batched_tokens` 跟 max-model-len 脫鉤，這個效應就幾乎消失了。（舊版行為是推論，沒有實測。）

**為什麼 131072 在 L4 上起不來**（粗算，實際數字以 vLLM 的錯誤訊息為準）：

```
每個 token 的 KV cache = 2 (K 和 V) × 28 層 × 8 個 KV head × 128 維 × 2 bytes (bf16)
                      = 114,688 bytes ≈ 112 KB
一條 131072 token 的序列 ≈ 112 KB × 131072 ≈ 14 GiB

L4 可用記憶體        ≈ 22.5 GiB × 0.85 ≈ 19 GiB
  − bf16 權重                          ≈ 6 GiB
  − r=256 的 LoRA 槽位、activation、CUDA graph 等  ≈ 2–3 GiB
  = 能給 KV cache 的                   ≈ 10–11 GiB   <  14 GiB
```

**vLLM 的規則是：KV cache 至少要放得下「一條最長的序列」，否則拒絕啟動。** 這比「起來了但很慢」好得多 —— 它在啟動時就告訴你設定不合理，而不是讓你在壓測時才發現。

**預設值在這張卡上不是「慢」，是「根本不能用」** —— 這才是要調它的真正理由。

### 3.3 你應該觀察到什麼

| `--max-model-len` | KV cache size | Maximum concurrency | 飽和吞吐 | 為什麼 |
|---|---|---|---|---|
| 131072（預設） | — | — | — | **起不來**：一條序列的 KV cache 就超過可用記憶體 |
| 32768 | ≈ 相同 | 小 | ≈ 相同 | KV cache 按需分配，max-model-len 不預留空間 |
| 512 | ≈ 相同 | **大** | ≈ 相同 | 只有「最壞情況估計」變大 |

**如果你量到兩組吞吐幾乎一樣，那不是你做錯了，那就是正確答案。** 跟 Part 5 的量化一樣，這是一個比「調了變快 N 倍」更有價值的結論：它證明你理解 PagedAttention，而不是在背調校清單。

**如果兩組真的差很多**，先看 `KV cache size` 那行有沒有跟著變。有的話，代表這版 vLLM 的記憶體估算還是受 max-model-len 影響（例如 chunked prefill 被關掉了）。把 log 貼進 `RESULTS.md` —— 那也是一個值得記的發現。

**真正能動吞吐的旋鈕在 Part 4**（`--max-num-batched-tokens`、`--max-num-seqs`）。

### ⚠️ 陷阱

- **`--max-model-len` 設得比實際 prompt 短會直接報錯**（不是靜靜截斷，這點比 llama.cpp 好）。留一點餘裕，我們設 512 對 150+5 是很寬裕的。
- **改這個參數一定要重啟服務**，它是啟動時劃定的。
- **`--max-model-len` 設太大會拒絕啟動。** 用 `-d --rm` 背景起的話，容器一退出就被刪掉，`docker logs` 什麼都拿不到 —— 你只會看到壓測連不上。所以第一次試新的值時用前景模式跑。

---

## Part 4：其餘調校旋鈕

### 4.1 `--gpu-memory-utilization`

```bash
# 📖 精簡路線讀就好。預設 0.9。試 0.80 / 0.90 / 0.95
```

| 值 | 效果 | 風險 |
|---|---|---|
| 0.80 | 保守，留餘裕 | KV cache 較少，並發上限低 |
| 0.90（預設） | 平衡 | |
| 0.95 | 並發最大 | **尖峰時可能 CUDA OOM**，尤其在 k8s 裡有其他東西共用 GPU 時 |

**Phase 3 的伏筆**：如果用 GPU time-slicing 在一張卡上跑多個 pod，**每個 pod 都用 0.9 的話第二個一定 OOM**（它們共用同一塊 24GB，但 k8s 對此一無所知）。那時候這個值要除以 pod 數。

### 4.2 `--max-num-seqs`

📖 精簡路線讀就好。

同時在跑的序列數上限。調大 → 吞吐↑、個別延遲↑。L4 這級的卡（顯存 < 70 GiB）預設是 256（依 vLLM 原始碼 `vllm/engine/arg_utils.py`）。

對本 workload（極短輸出），這個值可以開得很大。試 `64 / 256 / 1024`，看吞吐在哪裡飽和。

### 4.3 `--max-num-batched-tokens`（chunked prefill）

一輪最多處理幾個 token。

**這個參數對 prefill 主導的 workload 特別重要**：我們的每個請求要 prefill 150 個 token，如果一輪只能處理 512 個 token，那一輪最多只能 prefill 3 個新請求。調大能讓更多請求同時進入 prefill。

代價是：一輪的時間變長，正在 decode 的請求要等更久 → **p99 會變差**。

**這就是 throughput 與 latency 的張力最直接的體現。**

**精簡路線實跑這一個旋鈕**（Phase 1 合併路線的 ④）：
- 預設值看 ① 的啟動 log：`docker logs vllm 2>&1 | grep max_num_batched_tokens`（印 `Chunked prefill is enabled with max_num_batched_tokens=N`）。依 vLLM 原始碼，顯存 < 70 GiB 的卡在 API server 模式預設 **2048**（未實測）—— 也就是一輪最多 prefill 約 13 個 150-token 的請求
- 用 ① 的完整指令加 `--max-num-batched-tokens 8192`（約 54 個）重啟，跑一次飽和（`--request-rate inf`）和一個固定 rate（取 ① 掃描曲線上翹前的那個點），跟 ① 比吞吐與 p99 TTFT
- 吞吐上去、p99 變差 → 典型取捨；吞吐不變 → 代表瓶頸不在批次大小（對照 Part 7 的判讀表）。**兩種都是可以寫進 `RESULTS.md` 的結論**

### 4.4 `--enforce-eager`

📖 精簡路線讀就好。關掉 CUDA graph。

| | CUDA graph（預設） | `--enforce-eager` |
|---|---|---|
| 啟動時間 | 慢（要編譯） | 快 |
| 執行效能 | 快 | 慢 |
| 記憶體 | 多佔一些 | 少 |

**幾乎不該在生產用 `--enforce-eager`**，但它在 debug 時有用（排除 CUDA graph 相關的問題），而且在**啟動時間敏感**的場景（例如你想縮短 k8s 的 `startupProbe` 容忍時間）值得量一下代價。

---

## Part 5：量化對照矩陣

### 5.1 做法

**bf16 基準：用 Phase 1 Part 6 的 merged 權重**，不是 runtime LoRA 那組。FP8 這組也是從 merged 權重量化，兩組之間只差「量化」這一個變數；拿 runtime LoRA 當基準的話，等於同時改了量化和 LoRA 方式兩個變數（違反 Part 1.2 第 4 條）。基準要用跟 FP8 同一套 `vllm bench serve` 參數重跑一次。

**FP8 —— 最省事的一組，不需要任何離線量化步驟**：

```bash
docker run --rm -d --name vllm --gpus all --ipc=host -p 8000:8000 \
  -v "$HOME/vllm-cache:/root/.cache/vllm" \
  -v /home/ubuntu/merged:/merged:ro \
  vllm/vllm-openai:latest \
    --model /merged \
    --served-model-name pricer \
    --quantization fp8 \
    --max-model-len 512 \
    --gpu-memory-utilization 0.85
```

> L4 是 Ada 架構（compute capability 8.9），**有 FP8 硬體支援**。這是選 g6 而不是 g5 的原因 —— A10G 是 Ampere，沒有 FP8。

**AWQ / GPTQ（選作）**：需要離線量化步驟（`llm-compressor` 或 `autoawq`），而且要跑校準資料。做完 FP8 之後如果還有時間再說，**不做也不影響結論**。

### 5.2 每一組都要量兩件事

| 量化 | MAE | 飽和吞吐 | p95 TTFT | 權重大小 | KV cache size |
|---|---|---|---|---|---|
| bf16（merged） | | | | | |
| FP8（merged） | | | | | |
| AWQ（選作） | | | | | |

**權重大小不要看 `nvidia-smi`**：vLLM 啟動時就按 `--gpu-memory-utilization` 把顯存整塊佔走，所以 `nvidia-smi` 兩組都會顯示差不多的用量。要看啟動 log：

```bash
docker logs vllm 2>&1 | grep -E "Model loading took|KV cache size"
```

（確切措辭依版本而異。）權重變小的效果，會反映在 `KV cache size` 變大上。

**MAE 和吞吐一定要成對記錄。** 只看吞吐的話，你會得出「量化很棒」的結論；加上 MAE 才知道代價。

### 5.3 你可能會得到一個「反直覺」的結果

**量化不一定會讓這個 workload 變快。**

量化省的是**記憶體頻寬**。它在 decode 階段有明顯效益（每個 token 都要把整個模型權重從 HBM 讀一次）。但我們的輸出只有 5 個 token —— 絕大部分時間花在 prefill，而 prefill 是 **compute-bound**。

所以合理的結果是：
- **權重明顯變小** ✅（6.4GB → 約 3.6GB；embedding 層仍是 bf16，所以不會剛好減半）
- **吞吐提升有限** ⚠️
- **MAE 略有退化** ⚠️

**如果你量到「量化後幾乎沒變快」，那不是你做錯了，那就是正確答案。** 而且這是一個比「量化讓我快了 2 倍」更有價值的結論，因為它顯示你理解機制而不是在背結論。

**但顯存下降本身就是價值**：省下的空間可以換成更多 KV cache（更高並發），或者讓 Phase 3 的 time-slicing 能塞下更多 replica。**這才是量化對本 workload 的真正好處。**

---

## Part 6：多 LoRA 熱插拔

**目的**：這直接對應 on-prem 最常見的省 GPU 架構 —— 多租戶共用一個 base model。

### 6.1 做法

> **精簡路線**：Phase 1 合併路線的 ① 已經帶著 `pricer2=/adapter --max-loras 2` 起來了，不用另外重啟，直接跑下面的 `curl` 就好。底下的完整指令留作參考。

```bash
docker run --rm -d --name vllm --gpus all --ipc=host -p 8000:8000 \
  -v "$HF_HOME:/root/.cache/huggingface" \
  -v "$HOME/vllm-cache:/root/.cache/vllm" \
  -v /home/ubuntu/adapter:/adapter:ro \
  -e "HF_TOKEN=$HF_TOKEN" \
  vllm/vllm-openai:latest \
    --model meta-llama/Llama-3.2-3B \
    --served-model-name base \
    --enable-lora \
    --lora-modules pricer=/adapter pricer2=/adapter \
    --max-lora-rank 256 \
    --max-loras 2 \
    --max-model-len 512

curl -s localhost:8000/v1/models | python3 -c "
import json,sys
print([m['id'] for m in json.load(sys.stdin)['data']])"
# ['base', 'pricer', 'pricer2']
```

同一個服務、同一份 base 權重，三個模型名稱都能打。

### 6.2 為什麼這是 on-prem 的關鍵能力

```
傳統做法：           多 LoRA 做法：
┌─────────┐         ┌─────────────────────┐
│ 團隊A模型│ GPU 1   │  一份 base 權重      │ GPU 1
├─────────┤         │  + adapter A (0.5GB)│
│ 團隊B模型│ GPU 2   │  + adapter B (0.5GB)│
├─────────┤         │  + adapter C (0.5GB)│
│ 團隊C模型│ GPU 3   └─────────────────────┘
└─────────┘
  3 張卡                    1 張卡
```

**on-prem 的容量是固定的**（雲端可以再開一台，地端不行），所以這種省法的價值比在雲端高得多。

### 6.3 要量的東西（📖 精簡路線讀就好）

| 情境 | 吞吐 | p95 TTFT |
|---|---|---|
| 只服務 1 個 adapter | | |
| 同時服務 2 個 adapter（流量各半） | | |

**`--max-loras` 控制同時「駐留在 GPU 上」的 adapter 數。** 超過的會被換出換入，那個換頁成本就是代價。這也是一個值得量的取捨。

### ⚠️ 陷阱

- **`--max-lora-rank` 是所有 adapter 的共同上限**，要設成最大的那個。混用不同 rank 的 adapter 時，小 rank 的會浪費一些記憶體。
- **`--max-loras` 太小會造成頻繁換頁**，症狀是 p99 忽高忽低。

---

## Part 7：瓶頸判讀

**目的**：這是整個 Phase 2 的收束。面試問「你怎麼知道瓶頸在哪」，這一節就是答案。

### 7.1 三個訊號一起看

在壓測跑著的時候，同時開三個視窗：

```bash
# 視窗 1：GPU 使用率與顯存
nvidia-smi dmon -s um -d 1

# 視窗 2：vLLM 的內部狀態
watch -n 1 'curl -s localhost:8000/metrics | grep -E "num_requests_(running|waiting)|kv_cache_usage_perc"'

# 視窗 3：CPU（g6.xlarge 只有 4 vCPU）
top

# 視窗 4：壓測本身
docker exec vllm vllm bench serve ...
```

### 7.2 判讀表

| GPU util | KV cache 使用率 | 佇列長度 | 診斷 | 該調什麼 |
|---|---|---|---|---|
| 高（>90%） | 低 | 長 | **compute-bound** | 沒得調了，換更好的卡或量化到能加速的程度 |
| 中 | **接近 1.0** | 長 | **KV cache 不足** | 調小 `--max-model-len`、調高 `--gpu-memory-utilization` |
| 低 | 低 | 長，CPU 不滿 | **批次湊不起來** | 調大 `--max-num-seqs` / `--max-num-batched-tokens` |
| 低 | 低 | 短 | **客戶端才是瓶頸** | 壓測工具的並發不夠，加大 `--request-rate` |
| 低～中 | 低 | 長，且 `top` 看到 4 顆 vCPU 接近滿載 | **CPU-bound** | vLLM 的 API server、引擎和壓測工具都擠在 4 vCPU 上。記錄下來當作這台機型的限制；要確認的話把壓測工具搬到另一台機器跑 |

### 7.3 本 workload 的預期診斷

前面說過，這是 prefill 主導的 workload。照目前的設定壓測，你**應該**會看到：

- KV cache 使用率不高（序列很短）
- GPU util 很高
- 佇列還是會長

→ **compute-bound**。這代表你已經把這張卡榨乾了，剩下的路是換卡或改架構（例如把 prefill 和 decode 分開部署，那是進階題）。

### 7.4 ⚠️ 最重要的警告：`nvidia-smi` 的 GPU util 是騙人的

`nvidia-smi` 的 "GPU-Util" 只回答一個問題：**「過去這段取樣區間裡，有沒有任何 kernel 在執行？」**

它**不**告訴你那些 kernel 有沒有把算力用滿。一個只用了 5% SM 的 kernel，只要一直在跑，GPU-Util 就是 100%。

**Phase 3 會用 DCGM 的 `DCGM_FI_PROF_SM_ACTIVE` 和 `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` 來看真實的算力利用率。** 那時候你會看到 GPU-Util 100% 但 SM_ACTIVE 只有 40% 的情況 —— 那才是真相。

**「你怎麼知道 GPU 沒被浪費」這題，能答出這個區別的人非常少。**

---

## Phase 2 驗收清單

- [ ] 預設 131072 的啟動結果記錄了（起不來的話，記下錯誤訊息裡的可用 KV cache 與 estimated maximum model length）
- [ ] `--max-model-len` 兩組（32768 / 512）的 `KV cache size`、`Maximum concurrency` 與吞吐都記錄了
- [ ] `--max-num-batched-tokens` 預設值（從 log 讀）vs 8192 的吞吐與 p99 TTFT 記錄了
- [ ] request-rate 掃描曲線畫出來了，找到 p99 開始上翹的轉折點
- [ ] 量化對照表至少有 bf16 與 FP8 兩組，**每組都有 MAE 和吞吐**
- [ ] 多 LoRA：`/v1/models` 列出 `pricer` 與 `pricer2`，兩個都打得通（1 vs 2 的效能差異精簡路線跳過）
- [ ] 能用三個訊號（GPU util / KV cache / 佇列）說出當前是哪種瓶頸，**並附上截圖或數字**
- [ ] 所有數字進了 `RESULTS.md`
- [ ] **機器已 `stop`**

---

## 這一階段該產出的一句話

在 `92-interview.md` 裡補上這段（把問號換成你的實際數字）：

> 「我的 workload 是 prefill 主導的（輸入 150 token、輸出 5 token）。很多人以為 `--max-model-len` 是最大的槓桿，我實測的結果是：預設的 131072 在 L4 上根本起不來（一條序列的 KV cache 就要 14GB），但能起來的 32768 和 512 兩組吞吐只差 ?% —— 因為 vLLM 的 KV cache 是按需分配的，max-model-len 不會預留空間，它只影響啟動檢查和『最大並發』那行最壞情況的估計。真正的瓶頸是算力：壓測時 GPU util ?%、KV cache 使用率只有 ?%，但佇列還是在長。FP8 量化把權重從 6.4GB 降到 ?GB，但吞吐只提升 ?% —— 因為量化省的是記憶體頻寬，而 prefill 是 compute-bound。」

---

## 下一站

→ [`06-phase3-k3s-gpu.md`](06-phase3-k3s-gpu.md)：在同一台機器上裝 k3s，把 Phase 0/0.5 的 k8s 成果接上真 GPU。不開 EKS。
