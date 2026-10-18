# LLM Inference on Kubernetes 實驗室 — 92：面試敘事與追問應答

> 這一份要**邊做邊填**。每做完一個 Phase 就回來把問號換成你的實際數字。
> **沒有被你量過的東西，不要寫進來。** 追問兩層就會露餡，而且比不會更糟。

---

## 一、一分鐘版本（被問「介紹一下這個專案」）

> 「我上完一門 LLM 工程的課程，學會用 QLoRA 微調一個商品估價模型。但課程到『在 Modal 上跑起來』就結束了，我想把它真正部署成一個可維運的服務，所以自己做了這個 lab。
>
> 我本機沒有 NVIDIA GPU，所以拆成兩條線：**k8s 的部分全部在本機零成本做完**（單節點 OrbStack 建骨架和可觀測性，再用 kind 開三節點補排程、drain、資源治理），**只有真正需要 GPU 的部分才租雲端機器**，用 vLLM 服務化並做效能調校，最後在同一台機器上裝 k3s 把兩邊接起來。
>
> 整個過程我堅持一件事：**每個實驗都同時量正確性和效能**。因為這個任務有公開的 baseline（MAE $39.85），所以我隨時知道『服務有沒有壞掉』，而不是只看 health check。這件事救了我一次 —— 有個設定錯誤會讓服務完全正常但答案是錯的，只有 MAE 抓得到。」

**這段話的三個賣點**：務實的成本取捨、k8s 深度、以及「正確性優先」的工程判斷。

---

## 二、各 Phase 的一段話（填空）

### Phase 0 —— 本機 k8s 骨架

> 「我先在本機把整套 k8s 骨架做完，模型後端用 llama.cpp 當替身 —— 特意選它而不是 Ollama，因為它有 Prometheus `/metrics`，語意跟 vLLM 的指標對得上。這樣 ServiceMonitor、prometheus-adapter、custom metrics HPA 這些最花時間試錯的東西，我可以在零成本環境全部搞定，不用在按小時計費的 GPU 上 debug 純 k8s 的問題。
>
> 最關鍵的設計是三種 probe 的分工。LLM 服務啟動要 ? 秒，沒有 `startupProbe` 就會被 `livenessProbe` 無限重啟。我還刻意讓 `livenessProbe` 打 `/healthz`（不檢查後端）而 `readinessProbe` 打 `/readyz`（檢查後端）—— 如果 liveness 也去戳後端，後端一重啟就會害整排 API pod 被殺掉，把單一故障放大成連鎖故障。」

### Phase 0.5 —— kind 三節點

> 「單節點環境會給人錯誤的直覺，所以我另外開了三節點的 kind 叢集來矯正。最重要的一個是：`ReadWriteOnce` 的 PVC 在單節點上可以被多個 pod 同時掛載而且完全正常 —— 因為 RWO 的語意是『一個**節點**』不是『一個 pod』。我刻意在多節點上重現了跨節點掛載失敗 —— 在 kind 的 local-path 上，它是被 scheduler 以 `volume node affinity conflict` 擋在 `Pending`；換成 EBS 這類網路磁碟，則是排上了才在 `ContainerCreating` 撞 `Multi-Attach error`。同一個 RWO 語意，擋下來的時間點不同。
>
> 這個 Phase 我也把 on-prem 的資源治理練完了：ResourceQuota 限制 GPU 配額、PriorityClass 讓線上推論可以搶佔離線評估、PDB 保護節點維護時的可用性。有個技巧是我用 `kubectl proxy` PATCH node status 加了一個假的 extended resource，這樣排程、配額、搶佔的**全部語義**都能在沒有 GPU 的機器上練完，之後換成 `nvidia.com/gpu` 一行都不用改。」

### Phase 1 —— vLLM 服務化

> 「我刻意先用純 Docker 跑 vLLM，不一開始就上 k8s。因為 GPU 問題的排查順序是由下往上 —— `nvidia-smi` → `docker run --gpus all` → device plugin → pod。一開始就疊 k8s，出問題時分不清是哪一層。
>
> 這個階段我刻意對照了兩種失敗。我的 adapter 是 r=256，vLLM 的 `--max-lora-rank` 預設是 16 —— 不設的話 vLLM **啟動時就拒絕**，這是大聲的錯誤，很好處理。真正危險的是無聲的那種：請求的 model 名稱打到 base，服務正常啟動、健康檢查通過、API 正常回應合法的價格數字，但 MAE 從 $40.74 變成 $200.29。**只有業務層面的正確性驗證抓得到這個錯誤。** 這件事讓我意識到推論服務的部署流程需要一道 MAE 門檻檢查，光有 health check 不夠。」

### Phase 2 —— 效能調校

> 「我的 workload 是 **prefill 主導**的：輸入約 150 token、輸出只有 5 token。這個特性推翻了很多 LLM 推論的『常識』。
>
> 很多人以為 `--max-model-len` 是最大的槓桿，我實測的結果是：預設的 131072 在 L4 上根本起不來 —— 一條 131072 token 序列的 KV cache 就要約 14GB，比扣掉權重後的可用空間還大，vLLM 直接拒絕啟動。但能起來的 32768 和 512 兩組，吞吐只差 0.4%（35.92 vs 35.77 req/s），KV cache 都是約 84,900 token。因為 vLLM 的 KV cache 是按需分配的（PagedAttention），max-model-len 不會替每個請求預留空間 —— 它只影響啟動檢查，和 log 裡『最大並發』那個最壞情況的估計。
>
> 真正的瓶頸是算力：飽和時 GPU util 100%、KV cache 只用了 13%，佇列卻排到 430。所以把 `--max-num-batched-tokens` 從 2048 調到 8192 吞吐也不變，反而讓 TPOT 慢了 2.8 倍。有效的是減少計算量：把 r=256 的 LoRA merge 進去，吞吐 +49%；再做 FP8，又 +37%，MAE 都沒變（$40.74 → $40.80 → $40.15）。FP8 會變快，是因為 L4 上 vLLM 選的是 W8A8 的 CUTLASS kernel，連運算都是 FP8 —— 『量化只省頻寬、對 compute-bound 沒用』只對 weight-only 量化成立。權重變小（6.04 → 3.57 GiB）也有價值 —— 在一張卡上跑多個 replica 時，真正的限制是「每個 pod 都要一份權重」，不是 KV cache。
>
> 我還差點被 prefix cache 騙：壓測工具預設用同一個 seed，第二次跑命中率 96%，吞吐從 36 跳到 258 req/s。所以我的量測規則是熱身與正式一定用不同的 seed。」

### Phase 3 —— GPU 上的 k8s

> 「我沒有用 EKS。GPU 加 k8s 的核心知識在單機 k3s 上完全學得到，而且更貼近 on-prem 的場景，還省掉每月 $73 的控制平面租金。
>
> 單張卡讓我撞到一個真實的死結：`maxSurge: 1 / maxUnavailable: 0` 的滾動更新永遠卡住，因為新 pod 等不到 GPU，而舊 pod 要等新 pod 就緒才會被關掉。`nvidia.com/gpu` 不可切分也不可超賣。三個選項各有代價 —— 接受 ? 秒停機、多買一張卡、或用 time-slicing。我用 time-slicing 把一張 L4 宣告成 3 張，然後把代價量出來：總吞吐 ?，但 **p99 從 ? 惡化到 ?**。而且 k8s 以為有 3 張卡，實際上 bf16 權重只塞得下 2 個 pod —— 第 3 個 pod 排程成功、啟動時才因為顯存不夠失敗，scheduler 完全看不到這件事。
>
> 還有一件事我覺得最有價值：`nvidia-smi` 的 GPU-Util 是騙人的。它只回答『有沒有 kernel 在跑』，不回答『算力有沒有被用滿』。我用 DCGM 的 `SM_ACTIVE` 和 `PIPE_TENSOR_ACTIVE` 量到 GPU-Util 是 ?% 但 SM_ACTIVE 只有 ?% —— 這個落差才是真正可行動的資訊。」

---

## 三、高頻追問與答法

### Q：你怎麼知道瓶頸在哪？

三個訊號一起看，缺一不可：

| GPU util | KV cache | 佇列 | 診斷 |
|---|---|---|---|
| 高 | 低 | 長 | compute-bound |
| 中 | 接近 1.0 | 長 | KV cache 不足 |
| 低 | 低 | 長 | 批次湊不起來 |
| 低 | 低 | 短 | 壓測客戶端才是瓶頸 |

**加分**：「而且 GPU util 本身要用 DCGM 的 SM_ACTIVE 看，`nvidia-smi` 的數字只代表有 kernel 在跑。」

### Q：為什麼 HPA 不能看 CPU？

> 「LLM 的瓶頸是 GPU 或記憶體頻寬。一個完全飽和、請求排到天邊的 vLLM pod，CPU 使用率可能只有 20%。我實際做過 CPU HPA，它完全不觸發。正解是用佇列長度（`vllm:num_requests_waiting`）透過 prometheus-adapter 變成 custom metric。」

**追問「還有什麼要注意的」時的加分答案**：
> 「`behavior.scaleUp.stabilizationWindowSeconds` 必須大於冷啟動時間。新 pod 要 ? 秒才 ready，如果穩定視窗比這個短，HPA 會以為擴容沒效果而持續擴到上限 —— 這是控制理論的積分飽和。我設 180 秒。縮容我設更保守的 600 秒，因為縮錯的代價是再等一次冷啟動。」

### Q：模型權重怎麼分發到多個節點？

> 「四種做法的光譜，沒有一個全面最佳：
> 1. **烤進 image** —— 版本不可變、air-gap 友善，但 image 變成數百 GB
> 2. **RWX 共享（NFS/Ceph）** —— 只存一份，但 NFS 是單點故障也是頻寬瓶頸
> 3. **每節點 local PV + 預先分發** —— 冷啟動最快，但要自己管一致性
> 4. **initContainer 從內部物件儲存拉到 local PV** —— 最常見的生產做法
>
> 我在 lab 裡用了 RWX，但我知道對『模型權重』來說它其實不是好選擇 —— 權重是唯讀、大檔、順序讀取，每次 pod 啟動都從 NFS 讀會把網路打爆。RWX 適合的是需要多寫者共享的小檔案。」

### Q：GPU 怎麼在多個團隊之間分配？

> 「這是 on-prem 跟雲端差最多的地方 —— **地端的容量是固定的，不能像 Karpenter 那樣不夠就開一台**。所以重心在資源治理：
> - `ResourceQuota` 限制每個 namespace 的 `requests.nvidia.com/gpu`
> - `LimitRange` 給預設值（否則有 quota 之後，沒寫 requests 的 pod 會被直接拒絕）
> - `PriorityClass` 讓線上推論可以搶佔離線評估，而且離線那個要設 `preemptionPolicy: Never`，不然兩個 batch 會互相搶佔變成活鎖
> - GPU 共享（time-slicing / MIG）的價值遠高於雲端」

### Q：time-slicing 有什麼問題？

> 「四個，而且都很嚴重：
> 1. **完全沒有記憶體隔離** —— 共用同一塊 HBM，而 k8s 對此一無所知。兩個 pod 都用預設的 `--gpu-memory-utilization 0.9` 就會讓第二個直接 CUDA OOM
> 2. **沒有故障隔離** —— 一個 XID error 同卡全死
> 3. **`nvidia.com/gpu: 3` 是謊報**，scheduler 對記憶體壓力毫無感知
> 4. **效能非線性** —— 我量到總吞吐 ?，但 p99 明顯惡化
>
> 隔離強度的光譜是：time-slicing（無隔離）→ MPS（記憶體軟上限）→ MIG（硬體切分）→ 整卡獨佔。SLA 敏感的多租戶該用 MIG，但我的 L4 是 Ada 架構不支援（只有 A100/H100 那條線有），所以我只做過 time-slicing，MIG 我能講差異但沒跑過。」

**最後那句誠實的補充很重要。** 它讓前面的內容變得可信。

### Q：pod 一直重啟你怎麼查？

> 「先看 `kubectl describe pod` 的 `Last State`，不是 `kubectl logs`：
> - **Exit Code 137** = OOMKilled，而且 log 裡什麼都沒有（被核心直接砍）
> - **log 看起來正常但一直重啟** = `startupProbe` 的容忍時間不夠（`failureThreshold × periodSeconds`）
> - **卡在 Init** = initContainer 失敗，要用 `kubectl logs -c <initContainer名>` 才看得到 log
>
> 這三個我都刻意在 lab 裡重現過並寫成 post-mortem。」

### Q：這跟真正的 production 差在哪？

**這題一定要誠實答，而且要具體。**

> 「三個最大的缺口：
> 1. **沒有真正的多節點推論。** 我的叢集最多三個節點而且都是 CPU，GPU 只有一張卡。跨節點的 tensor parallel、NCCL/RDMA 調優、NUMA 與 PCIe 拓撲這些我沒碰過。如果要做，我會用 LeaderWorkerSet 加 Ray，而且第一步是量 NCCL all-reduce 的頻寬來決定 TP/PP 怎麼切。
> 2. **沒有 day-2 operations 的時間深度。** 一個月的 lab 產不出 k8s 版本升級、GPU driver 升級（要 drain 全節點）、etcd 備份還原這些經驗。
> 3. **規模是玩具等級。** 單模型、沒有真實的多租戶競爭、沒有成本歸屬、沒有模型版本的 A/B。
>
> 我把這些整理在 repo 的 `KNOWN-GAPS.md`，每一項都寫了『它解決什麼問題 / 我會怎麼做 / 為什麼這次沒做』。」

---

## 四、你的三份 post-mortem（面試主打）

**把 `POSTMORTEMS.md` 準備好，面試時可以直接打開。**

掏出 post-mortem 比掏出架構圖有用十倍，因為它證明的是**你排查過**，不只是部署過。

每份的格式：現象 → 排查路徑（包含查錯方向的） → 根因 → 修法 → 預防。

**「排查路徑」那一段要誠實寫下走錯的岔路。** 一份沒有岔路的 post-mortem 看起來像編的。

---

## 五、面試前 30 分鐘的複習順序

1. [`00-concepts.md`](00-concepts.md) 的 Part 6（LLM 服務跟一般服務的十個差異）—— 這是全文件密度最高的一頁
2. `RESULTS.md` —— **把你自己的數字記熟**，講不出數字的專案敘事沒有說服力
3. `POSTMORTEMS.md` —— 三個故障的排查路徑
4. `KNOWN-GAPS.md` —— 知道自己的邊界在哪，被問到時能大方承認

---

## 六、沒實跑、但要講得出來的陷阱（精簡路線 📖 項目）

面試被問到時，誠實說「這個我沒親手做，但我知道陷阱在哪」，然後講下面那句。**不要說成做過。**

| 主題 | 一句話 | 出處 |
|---|---|---|
| RWX / 模型分發 | RWX 不適合模型權重：唯讀大檔、啟動時順序讀一次 → 用內部物件儲存 + initContainer 拉到 local PV；NFS 會變成頻寬瓶頸與單點故障 | docs/03 Part 3 |
| `volumeBindingMode` | local PV 必須 `WaitForFirstConsumer`，`Immediate` 會把 PV 建在沒有 GPU 的節點上 | docs/03 Part 3.5 |
| MetalLB | on-prem 的 LoadBalancer 會一直 `<pending>`；MetalLB 的 L2 模式是**故障轉移不是負載平衡**（流量全走一個節點），要真分流得用 BGP | docs/03 Part 4 |
| CronJob | `concurrencyPolicy` 預設 `Allow` 會越積越多；不設 `startingDeadlineSeconds` 停機後會補跑；沒有 `ttlSecondsAfterFinished` 會讓 etcd 膨脹 | docs/03 Part 10 |
| NetworkPolicy | CNI 不執行時 apply 成功但完全沒效果（kindnet 就是）；model-server 適合幾乎全封，只放 pricer-api 進、DNS 出 | docs/03 Part 11 |
| `--max-lora-rank` | 預設 16，adapter 是 r=256 → vLLM **啟動就失敗**（大聲失敗，好處理）。危險的是反面：請求打到 base，一切綠燈但 MAE 變 $200 | docs/04 Part 5 |
| `--gpu-memory-utilization` | time-slicing 時每個 pod 的值要除以 pod 數，而且每份都要放得下權重 + 開銷；k8s 對顯存一無所知 | docs/05 Part 4.1 |
| `--max-num-seqs` | 同時跑的序列上限，調大吞吐↑、個別延遲↑；L4 這級的卡預設 256 | docs/05 Part 4.2 |
| `--enforce-eager` | 關 CUDA graph：啟動快、執行慢；只在 debug 或啟動時間敏感時用 | docs/05 Part 4.4 |
| `/dev/shm` | k8s 預設 64MB，vLLM 負載一上來就 crash → `emptyDir: {medium: Memory}`，而且它算進記憶體 limit | docs/06 Part 5 |
| time-slicing 第 3 個 pod | `nvidia.com/gpu: 3` 讓第 3 個 pod **排程成功**，然後因顯存不夠 CrashLoop —— 死結還在，只是卡在 k8s 看不到的地方 → 這是 MIG 存在的理由 | docs/06 Part 7.4 |
| Helm | `helm upgrade` 不更新 CRD；`--set` 蓋過 `-f`；`uninstall` 不刪 CRD 與 PVC | docs/06 Part 10 |

---

## ⚠️ 兩條紅線

1. **不要宣稱你沒量過的東西。** 尤其是 NetworkPolicy（CNI 不執行的話你寫的 YAML 根本沒作用）和 MIG（L4 不支援）。
2. **不要把 Ed Donner 課程的 adapter 說成是自己訓練的。** 正確的說法是：「模型和資料集來自我上的課程，我做的是把它部署成可維運的服務。」—— 等你自己跑完 week7 的 fine-tune，再把 `k8s/base/model.conf` 換成自己的，那時候敘事就變成完整的「學會微調 → 自己部署上線」。
